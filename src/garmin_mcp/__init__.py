import logging
import os
import sys
import threading

import anyio
import uvicorn
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse

from garmin_mcp import (
    activity_management,
    challenges,
    data_management,
    devices,
    gear_management,
    health_wellness,
    nutrition,
    training,
    user_profile,
    weight_management,
    womens_health,
    workout_templates,
    workouts,
    workout_builders,
    courses,
    activity_analysis,
    calendar_events,
)
from garmin_mcp.github_oauth_provider import GitHubOAuthProvider
from garmin_mcp.garmin_auth import initialize_garmin, safe_error
from garmin_mcp.token_store import TokenStoreError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

is_cn = os.getenv("GARMIN_IS_CN", "false").lower() == "true"

SERVER_URL = os.getenv("SERVER_URL", "")


# --- Tool filtering ---------------------------------------------------------
# Optionally expose only a subset of tools, to reduce the context an LLM must
# carry. No modules are removed; tools are simply not registered when filtered.
#   GARMIN_ENABLED_TOOLS  - comma-separated allowlist; if set, ONLY these register
#   GARMIN_DISABLED_TOOLS - comma-separated denylist; ignored if an allowlist is set
# Tool names are case-insensitive. Unset = all tools register (default behaviour).
def _parse_tool_set(value):
    if not value:
        return set()
    return {name.strip().lower() for name in value.split(",") if name.strip()}


def _resolve_tool_filters():
    """Read and validate tool filter environment variables at server startup."""
    enabled_value = os.getenv("GARMIN_ENABLED_TOOLS")
    enabled_tools = _parse_tool_set(enabled_value)
    if enabled_value and enabled_value.strip() and not enabled_tools:
        raise ValueError(
            "Invalid GARMIN_ENABLED_TOOLS: expected at least one tool name"
        )
    disabled_tools = _parse_tool_set(os.getenv("GARMIN_DISABLED_TOOLS"))
    return enabled_tools, disabled_tools


# Default per-call timeout (seconds). Garmin's API occasionally stalls a single
# request indefinitely; without a bound the blocking client call hangs until the
# MCP client's own timeout (~4 min) fires, reporting the whole server as
# unresponsive (see issue #248). 90s sits comfortably above a normal slow call
# yet well below that ceiling. Override with GARMIN_MCP_CALL_TIMEOUT; set 0 to
# disable the bound entirely.
_DEFAULT_CALL_TIMEOUT = 90.0


def _resolve_call_timeout() -> float:
    """Read GARMIN_MCP_CALL_TIMEOUT; fall back to the default on bad/absent input.

    A value <= 0 disables the timeout (returns 0.0).
    """
    raw = os.getenv("GARMIN_MCP_CALL_TIMEOUT")
    if raw is None or not raw.strip():
        return _DEFAULT_CALL_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(
            f"Invalid GARMIN_MCP_CALL_TIMEOUT {raw!r}; using default "
            f"{_DEFAULT_CALL_TIMEOUT}s.",
            file=sys.stderr,
        )
        return _DEFAULT_CALL_TIMEOUT
    return value if value > 0 else 0.0


class _GarminProxy:
    """Wraps the Garmin client to bound call duration and clarify runtime errors.

    Two jobs:

    1. Timeout: each client call runs on a daemon worker thread and is abandoned
       if it does not return within the configured timeout (issue #248 — an
       occasional Garmin request stalls forever and the blocking call would
       otherwise hang the whole server until the MCP client gives up minutes
       later). A stalled call raises a clear, retry-able error instead; the
       abandoned daemon thread dies with the process and never blocks shutdown.
       Such stalls are rare and transient, so a fresh thread per call is cheap
       relative to the network round-trip it guards.

    2. Error translation: token expiry or rate-limiting during a tool call would
       otherwise surface a raw library traceback. Known Garmin exceptions are
       translated to fixed messages without response bodies or token values.
    """

    def __init__(self, client, timeout=None):
        self._client = client
        self._lock = threading.RLock()
        self._timeout = _resolve_call_timeout() if timeout is None else timeout

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def _invoke(*args, **kwargs):
            # Serialize calls, including any worker that outlives its timeout.
            # A timed-out mutation may still complete; never replay it here.
            with self._lock:
                try:
                    return attr(*args, **kwargs)
                except (GarminConnectAuthenticationError, GarminConnectConnectionError,
                        GarminConnectTooManyRequestsError, TokenStoreError) as exc:
                    raise safe_error(exc) from None

        def _call(*args, **kwargs):
            if not self._timeout:
                return _invoke(*args, **kwargs)

            # Run on a daemon thread and join with a timeout. The worker's
            # return value or exception is captured and replayed in the caller
            # so translated Garmin errors propagate unchanged.
            outcome = {}

            def _worker():
                try:
                    outcome["value"] = _invoke(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - replayed below
                    outcome["error"] = exc

            worker = threading.Thread(
                target=_worker, name=f"garmin-call:{name}", daemon=True
            )
            worker.start()
            worker.join(self._timeout)
            if worker.is_alive():
                raise TimeoutError(
                    f"Garmin request '{name}' did not return within "
                    f"{self._timeout:g}s and was abandoned. This is usually a "
                    f"transient stall. The request may still complete; check "
                    f"its result before retrying a write. "
                    f"(Adjust with GARMIN_MCP_CALL_TIMEOUT, or set it to 0 to "
                    f"disable the limit.)"
                )
            if "error" in outcome:
                raise outcome["error"]
            return outcome.get("value")

        return _call


# Tools that read or write arbitrary paths on the server's filesystem. Useful for
# a local single-user install, but on a remote server a single (possibly
# prompt-injected) call could overwrite the OAuth token store, the Garmin token
# or the application code. They are never registered, whatever the filter says.
BLOCKED_TOOLS = frozenset({
    "download_activity_file",
    "download_course_gpx",
    "set_fit_download_dir",
    "upload_course",
})


class _ToolFilter:
    """Wraps a FastMCP app to conditionally register tools by function name.

    Modules register via ``@app.tool()``; we intercept that decorator and skip
    registration for any tool not permitted by the env-var filter. All other
    attribute access (``run``, ``resource``, ...) passes through to the app.
    """

    def __init__(self, app, enabled, disabled):
        self._app = app
        self._enabled = enabled
        self._disabled = disabled
        self._seen = set()  # tool names encountered, for typo detection

    def _allowed(self, name):
        name = name.lower()
        if name in BLOCKED_TOOLS:
            return False
        if self._enabled:
            return name in self._enabled
        return name not in self._disabled

    def tool(self, *args, **kwargs):
        # Most tools return json.dumps(...) as str. FastMCP's default
        # structured_output auto-detection then wraps that string again as
        # structuredContent {"result": "<escaped json>"}, doubling payload
        # size for clients that forward both blocks (issue #331). Opt out
        # globally; callers can still pass structured_output=True explicitly.
        kwargs.setdefault("structured_output", False)
        decorator = self._app.tool(*args, **kwargs)
        # Prefer the explicit registered name if given (@app.tool(name="x")),
        # so the env-var filter matches what the user actually configures.
        explicit = kwargs.get("name") or (
            args[0] if args and isinstance(args[0], str) else None
        )

        def wrapper(fn):
            name = explicit or getattr(fn, "__name__", "")
            self._seen.add(name.lower())
            if self._allowed(name):
                return decorator(fn)
            return fn  # skip registration; tool never reaches the LLM

        return wrapper

    def unknown_filter_names(self):
        """Configured names that never matched a real tool (likely typos)."""
        configured = self._enabled or self._disabled
        return sorted(configured - self._seen)

    def __getattr__(self, item):
        return getattr(self._app, item)

_MODULES = [
    activity_management,
    challenges,
    data_management,
    devices,
    gear_management,
    health_wellness,
    nutrition,
    training,
    user_profile,
    weight_management,
    womens_health,
    workouts,
    workout_builders,
    courses,
    activity_analysis,
    calendar_events,
]


def _build_app() -> tuple[FastMCP, GitHubOAuthProvider]:
    github_client_id = os.getenv("GITHUB_CLIENT_ID", "")
    github_client_secret = os.getenv("GITHUB_CLIENT_SECRET", "")

    if not github_client_id or not github_client_secret:
        logger.error("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET must be set.")
        sys.exit(1)

    if not SERVER_URL:
        logger.error("SERVER_URL must be set to the public base URL of this service.")
        sys.exit(1)

    oauth_provider = GitHubOAuthProvider(
        github_client_id=github_client_id,
        github_client_secret=github_client_secret,
        server_url=SERVER_URL,
    )

    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(SERVER_URL),
        resource_server_url=AnyHttpUrl(SERVER_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    mcp_app = FastMCP(
        "Garmin Connect MCP",
        auth_server_provider=oauth_provider,
        auth=auth_settings,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )

    # GitHub redirects here after user login
    @mcp_app.custom_route("/auth/callback", methods=["GET"])
    async def github_callback(request: Request):
        return await oauth_provider.handle_github_callback(request)

    @mcp_app.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp_app, oauth_provider


def init_api() -> Garmin:
    try:
        return initialize_garmin(is_cn=is_cn)
    except (GarminConnectAuthenticationError, GarminConnectConnectionError,
            GarminConnectTooManyRequestsError, TokenStoreError) as exc:
        logger.error("%s", safe_error(exc))
        sys.exit(1)


async def _serve(mcp_app: FastMCP) -> None:
    starlette_app = mcp_app.streamable_http_app()
    config = uvicorn.Config(
        starlette_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        log_level="info",
        access_log=False,  # OAuth callback URLs contain one-time credentials
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    try:
        enabled_tools, disabled_tools = _resolve_tool_filters()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    garmin = _GarminProxy(init_api())
    mcp_app, _ = _build_app()
    app = _ToolFilter(mcp_app, enabled_tools, disabled_tools)

    for module in _MODULES:
        module.configure(garmin)
        module.register_tools(app)

    workout_templates.register_resources(app)

    if enabled_tools:
        logger.info("Tool filter: allowlist of %d tool(s).", len(enabled_tools))
    elif disabled_tools:
        logger.info("Tool filter: denylist of %d tool(s).", len(disabled_tools))
    logger.info("Blocked filesystem tools (never registered): %s", ", ".join(sorted(BLOCKED_TOOLS)))
    unknown = [n for n in app.unknown_filter_names() if n not in BLOCKED_TOOLS]
    if unknown:
        logger.warning("Tool filter: name(s) not found and ignored: %s", ", ".join(unknown))

    logger.info(
        "GitHub OAuth enabled — only user id '%s' can authenticate.",
        os.getenv("GITHUB_ALLOWED_USER_ID") or f"(login fallback: {os.getenv('GITHUB_ALLOWED_USER') or 'not configured'})",
    )
    anyio.run(_serve, mcp_app)
