#!/usr/bin/env python3
"""
bot/bot.py — MikroTik Matrix Gateway Bot

Listens for Matrix messages produced by matrix-commander-rs (``--listen
forever --output json``), parses commands of the form:

    !mtik <router_id> <command>

Transport selection is automatic based on ``port`` in config.yaml:

    port 443 / 80            → RouterOS REST API (HTTPS/HTTP)  — RouterOS 7.1+
    port 8729                → RouterOS API over TLS            — RouterOS 6.x+
    port 8728 (or any other) → RouterOS API plaintext           — RouterOS 3.x+

Security model
--------------
Layer 0 — Identity & room gate:
    * Messages from the bot's own Matrix account are silently dropped
      (prevents feedback loops).
    * Messages from rooms other than ``command_room`` are silently dropped.
    * Messages from users not in ``allowed_users`` trigger an alert to
      ``admin_room`` and receive no response.

Layer 1 — Input validation:
    * All input is matched against CMD_RE before any further processing.
    * router_id is restricted to [A-Za-z0-9_-] — no shell metacharacters.
    * command is restricted to printable ASCII — no control characters.

Layer 2 — Command whitelist:
    * API path must be listed in ``allowed_commands`` from config.yaml.
    * Unknown paths are rejected before any network I/O.

Layer 3 — Write gate:
    * Write operations (=key=value params) require ALLOW_WRITES=true env var.
    * Default is read-only regardless of command whitelist.

Layer 4 — Transport security:
    * REST: path validated against [A-Za-z0-9/_-], params sent as JSON body.
    * librouteros: params passed as structured kwargs — no shell involved.
    * Credentials are never logged.

Layer 5 — Process & filesystem:
    * Runs as UID 10001. Read-only rootfs. config.yaml mounted :ro.
    * subprocess calls use argument lists, never shell=True.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

import librouteros
import librouteros.query
import requests
import urllib3
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH: Path = Path(os.getenv("ROUTERS_CONFIG", "/home/bot/config/config.yaml"))
MC_BIN: str = "/usr/local/bin/matrix-commander-rs"

# Matches: !mtik <router_id> <command_and_args>
#   router_id : 1–64 chars, alphanumeric / hyphen / underscore.
#   command   : 1–512 printable ASCII chars (no control chars).
CMD_RE: re.Pattern[str] = re.compile(
    r"^!mtik\s+(?P<router_id>[A-Za-z0-9_-]{1,64})\s+(?P<command>[\x20-\x7E]{1,512})$"
)

# Ports that indicate the RouterOS REST API transport.
REST_PORTS: frozenset[int] = frozenset({80, 443})

# Port that indicates RouterOS API over TLS (librouteros).
ROUTEROS_API_TLS_PORT: int = 8729

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
log = logging.getLogger("matrix-mikrotik-bot")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

RouterConfig = dict[str, Any]
RoutersMap = dict[str, RouterConfig]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class BotConfig:
    """Holds the full validated configuration loaded from config.yaml."""

    def __init__(
        self,
        bot_user: str,
        command_room: str,
        admin_room: str,
        allowed_users: list[str],
        allowed_commands: list[str],
        routers: RoutersMap,
    ) -> None:
        self.bot_user = bot_user
        self.command_room = command_room
        self.admin_room = admin_room
        self.allowed_users = allowed_users
        self.allowed_commands = allowed_commands
        self.routers = routers


def load_config(path: Path) -> BotConfig:
    """Load and validate the full configuration from config.yaml.

    Parameters
    ----------
    path:
        Filesystem path to the YAML configuration file.

    Returns
    -------
    BotConfig
        Fully validated configuration object.

    Raises
    ------
    SystemExit
        On missing file, YAML parse failure, or schema violation.
    """
    if not path.exists():
        log.critical("Config file not found: %s", path)
        sys.exit(1)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        log.critical("YAML parse error in %s: %s", path, exc)
        sys.exit(1)

    if not raw:
        log.critical("Config file is empty: %s", path)
        sys.exit(1)

    # --- required top-level security fields ---
    for field in ("bot_user", "command_room", "admin_room"):
        if not raw.get(field):
            log.critical("Missing required config field: %r", field)
            sys.exit(1)

    allowed_users: list[str] = raw.get("allowed_users", [])
    if not allowed_users:
        log.critical("allowed_users must contain at least one Matrix user ID")
        sys.exit(1)

    allowed_commands: list[str] = raw.get("allowed_commands", [])
    if not allowed_commands:
        log.critical("allowed_commands must contain at least one path")
        sys.exit(1)

    # --- validate allowed_commands entries ---
    for cmd in allowed_commands:
        if not re.fullmatch(r"[A-Za-z0-9/_-]+", cmd):
            log.critical("Invalid allowed_commands entry %r: must match [A-Za-z0-9/_-]+", cmd)
            sys.exit(1)

    # --- routers ---
    routers: dict[str, Any] = (raw or {}).get("routers", {})
    if not routers:
        log.critical("No routers defined in %s", path)
        sys.exit(1)

    required_keys = {"host", "port", "username", "password"}
    for rid, cfg in routers.items():
        missing = required_keys - set(cfg.keys())
        if missing:
            log.critical("Router %r is missing required keys: %s", rid, missing)
            sys.exit(1)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid):
            log.critical("Invalid router_id %r: must match [A-Za-z0-9_-]{1,64}", rid)
            sys.exit(1)

    log.info("Loaded %d router(s): %s", len(routers), sorted(routers.keys()))
    log.info("Allowed users: %s", allowed_users)
    log.info("Allowed commands: %d paths", len(allowed_commands))

    return BotConfig(
        bot_user=raw["bot_user"],
        command_room=raw["command_room"],
        admin_room=raw["admin_room"],
        allowed_users=allowed_users,
        allowed_commands=allowed_commands,
        routers=routers,
    )


# ---------------------------------------------------------------------------
# Transport: RouterOS REST API (RouterOS 7.1+, ports 80/443)
# ---------------------------------------------------------------------------


class RouterOSError(Exception):
    """Raised when any router transport returns an error or is unreachable."""


def _rest_session(cfg: RouterConfig) -> requests.Session:
    """Build an authenticated requests.Session for the RouterOS REST API."""
    session = requests.Session()
    session.auth = (cfg["username"], cfg["password"])
    verify: bool = cfg.get("tls_verify", True)
    session.verify = verify
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def _parse_ros_kv(raw: str) -> dict[str, str]:
    """Parse RouterOS =key=value token pairs into a plain dict.

    Parameters
    ----------
    raw:
        String of the form ``=address=10.0.0.1/24 =interface=ether1``.
    """
    return {k: v for k, v in re.findall(r"=([^=\s]+)=([^\s]*)", raw)}


def execute_rest(cfg: RouterConfig, raw_command: str) -> str:
    """Execute a command via the RouterOS REST API (RouterOS 7.1+).

    Parameters
    ----------
    cfg:
        Router configuration dict.
    raw_command:
        Validated command string from the Matrix message.

    Returns
    -------
    str
        JSON-formatted result, truncated to 4 000 chars.

    Raises
    ------
    RouterOSError
        On connection failure, auth error, or non-2xx response.
    """
    allow_writes: bool = os.getenv("ALLOW_WRITES", "false").lower() == "true"

    parts = raw_command.strip().split(None, 1)
    api_path = parts[0].strip("/")

    if not re.fullmatch(r"[A-Za-z0-9/_-]+", api_path):
        raise RouterOSError(f"Invalid API path: {api_path!r}")

    tls: bool = cfg.get("tls_verify", True)
    scheme = "https" if (tls or cfg["port"] == 443) else "http"
    url = f"{scheme}://{cfg['host']}:{cfg['port']}/rest/{api_path}"

    session = _rest_session(cfg)
    method = "GET"
    body: dict[str, str] | None = None

    if len(parts) > 1 and parts[1].strip().startswith("="):
        if not allow_writes:
            raise RouterOSError(
                "Write operations are disabled. Set ALLOW_WRITES=true to enable."
            )
        method = "POST"
        body = _parse_ros_kv(parts[1])

    try:
        resp = session.request(method, url, json=body, timeout=(5, 15))
    except requests.exceptions.ConnectionError as exc:
        raise RouterOSError(
            f"Cannot reach {cfg['host']}:{cfg['port']}: {exc}"
        ) from exc
    except requests.exceptions.Timeout:
        raise RouterOSError(
            f"Timeout connecting to {cfg['host']}:{cfg['port']}"
        ) from None

    if resp.status_code == 401:
        raise RouterOSError("Authentication failed — check credentials in config.yaml")
    if not resp.ok:
        raise RouterOSError(f"API error {resp.status_code}: {resp.text[:200]}")

    try:
        formatted = json.dumps(resp.json(), indent=2)
    except ValueError:
        formatted = resp.text

    return formatted[:3950] + "\n… (truncated)" if len(formatted) > 4000 else formatted


# ---------------------------------------------------------------------------
# Transport: RouterOS API via librouteros (RouterOS 3.x+, ports 8728/8729)
# ---------------------------------------------------------------------------


def _routeros_api_connect(cfg: RouterConfig) -> librouteros.Api:
    """Open a librouteros connection to a router."""
    port: int = cfg["port"]
    use_tls: bool = port == ROUTEROS_API_TLS_PORT

    try:
        if use_tls:
            import ssl
            ctx = ssl.create_default_context()
            if not cfg.get("tls_verify", True):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = librouteros.connect(
                cfg["host"],
                username=cfg["username"],
                password=cfg["password"],
                port=port,
                ssl_wrapper=ctx.wrap_socket,
            )
        else:
            conn = librouteros.connect(
                cfg["host"],
                username=cfg["username"],
                password=cfg["password"],
                port=port,
            )
    except librouteros.exceptions.TrapError as exc:
        raise RouterOSError(f"Authentication failed: {exc}") from exc
    except OSError as exc:
        raise RouterOSError(f"Cannot reach {cfg['host']}:{port}: {exc}") from exc

    return conn


def _ros_path_to_api(api_path: str) -> tuple[str, ...]:
    """Convert a slash-separated path to a RouterOS API word tuple."""
    parts = [p for p in api_path.strip("/").split("/") if p]
    if not parts:
        raise RouterOSError("Empty API path")
    return ("/" + parts[0],) + tuple(parts[1:])


def execute_api(cfg: RouterConfig, raw_command: str) -> str:
    """Execute a command via the librouteros RouterOS API (RouterOS 3.x+)."""
    allow_writes: bool = os.getenv("ALLOW_WRITES", "false").lower() == "true"

    parts = raw_command.strip().split(None, 1)
    api_path = parts[0].strip("/")

    if not re.fullmatch(r"[A-Za-z0-9/_-]+", api_path):
        raise RouterOSError(f"Invalid API path: {api_path!r}")

    has_params = len(parts) > 1 and parts[1].strip().startswith("=")

    if has_params and not allow_writes:
        raise RouterOSError(
            "Write operations are disabled. Set ALLOW_WRITES=true to enable."
        )

    conn = _routeros_api_connect(cfg)

    try:
        path_words = _ros_path_to_api(api_path)
        menu = conn.path(*path_words)

        if has_params:
            kwargs = _parse_ros_kv(parts[1])
            result: list[dict[str, Any]] = list(menu.add(**kwargs))
        else:
            result = list(menu)

        formatted = json.dumps(result, indent=2, default=str)
    except librouteros.exceptions.TrapError as exc:
        raise RouterOSError(f"RouterOS API trap: {exc}") from exc
    finally:
        conn.close()

    return formatted[:3950] + "\n… (truncated)" if len(formatted) > 4000 else formatted


# ---------------------------------------------------------------------------
# Transport dispatcher
# ---------------------------------------------------------------------------


def execute_command(cfg: RouterConfig, raw_command: str) -> str:
    """Route a command to the correct transport based on port number."""
    if cfg["port"] in REST_PORTS:
        return execute_rest(cfg, raw_command)
    return execute_api(cfg, raw_command)


# ---------------------------------------------------------------------------
# Matrix I/O
# ---------------------------------------------------------------------------


def send_matrix_message(text: str, room: str | None = None) -> None:
    """Send a plain-text message to Matrix via matrix-commander-rs.

    Parameters
    ----------
    text:
        Message body.
    room:
        Matrix room ID or alias override.
    """
    cmd = [MC_BIN, "--output", "json", "--message", text]
    if room:
        cmd += ["--room", room]
    try:
        subprocess.run(cmd, check=True, timeout=30, capture_output=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        log.error(
            "Failed to send Matrix message: %s",
            exc.stderr.decode(errors="replace"),
        )
    except subprocess.TimeoutExpired:
        log.error("matrix-commander-rs send timed out")


def _parse_event(line: str) -> tuple[str | None, str | None, str | None]:
    """Extract (room_id, sender, body) from a matrix-commander-rs JSON line.

    Returns (None, None, None) for non-text-message events or parse errors.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None, None, None

    source = obj.get("source", {})
    if source.get("type") != "m.room.message":
        return None, None, None
    content = source.get("content", {})
    if content.get("msgtype") != "m.text":
        return None, None, None

    room_id: str | None = obj.get("room_id") or source.get("room_id")
    sender: str | None = source.get("sender")
    body: str | None = (content.get("body") or "").strip()
    return room_id, sender, body


# ---------------------------------------------------------------------------
# Help message
# ---------------------------------------------------------------------------


def _build_help(cfg: BotConfig) -> str:
    """Build the help message dynamically from allowed_commands."""
    routers_list = ", ".join(f"`{r}`" for r in sorted(cfg.routers.keys()))
    commands_list = "\n".join(f"  {c}" for c in sorted(cfg.allowed_commands))
    allow_writes = os.getenv("ALLOW_WRITES", "false").lower() == "true"
    writes_note = "✅ enabled" if allow_writes else "❌ disabled (set ALLOW_WRITES=true)"

    return (
        "**MikroTik Matrix Bot**\n\n"
        "**Usage:** `!mtik <router_id> <path> [=key=value ...]`\n\n"
        f"**Routers:** {routers_list}\n\n"
        f"**Write operations:** {writes_note}\n\n"
        f"**Allowed commands:**\n```\n{commands_list}\n```\n\n"
        "**Examples:**\n"
        "  `!mtik core-01 system/resource`\n"
        "  `!mtik branch-02 ip/address`\n"
        "  `!mtik core-01 ip/dhcp-server/lease`"
    )


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


def dispatch(body: str, room_id: str | None, sender: str | None, cfg: BotConfig) -> tuple[str, str | None]:
    """Parse a Matrix message and return (response, target_room).

    Layer 0 — identity, room, and user gates are applied here.
    Returns ("", None) to remain silent.

    Parameters
    ----------
    body:
        Raw message text.
    room_id:
        Matrix room ID the message came from.
    sender:
        Matrix user ID of the message author.
    cfg:
        Full bot configuration.

    Returns
    -------
    tuple[str, str | None]
        (response_text, target_room_id). Empty string means no response.
    """
    # --- Layer 0a: ignore own messages ---
    if sender == cfg.bot_user:
        return "", None

    # --- Layer 0b: ignore messages from rooms other than command_room ---
    if room_id != cfg.command_room:
        return "", None

    # --- Layer 0c: unauthorised user ---
    if sender not in cfg.allowed_users:
        log.warning("Unauthorized access attempt from %s in %s", sender, room_id)
        alert = (
            f"⚠️ **SECURITY WARNING**\n\n"
            f"Unauthorized user: `{sender}`\n"
            f"Room: `{room_id}`\n"
            f"Attempted command: `{body[:200]}`"
        )
        return alert, cfg.admin_room

    # --- help / start ---
    if body.strip().lower() in ("help", "start", "!mtik help", "!mtik start"):
        return _build_help(cfg), cfg.command_room

    # --- Layer 1: regex validation ---
    m = CMD_RE.match(body)
    if not m:
        return "", None

    router_id = m.group("router_id")
    command = m.group("command")

    # --- router lookup ---
    if router_id not in cfg.routers:
        known = ", ".join(f"`{r}`" for r in sorted(cfg.routers.keys()))
        return f"❌ Unknown router `{router_id}`. Known IDs: {known}", cfg.command_room

    # --- Layer 2: command whitelist ---
    api_path = command.strip().split(None, 1)[0].strip("/")
    if api_path not in cfg.allowed_commands:
        allowed = "\n".join(f"  {c}" for c in sorted(cfg.allowed_commands))
        return (
            f"❌ Command `{api_path}` is not in the allowed list.\n"
            f"Allowed commands:\n```\n{allowed}\n```"
        ), cfg.command_room

    log.info("Dispatching: sender=%s router=%s command=%r", sender, router_id, command)

    try:
        result = execute_command(cfg.routers[router_id], command)
        return f"✅ `{router_id}` → `{command}`\n```\n{result}\n```", cfg.command_room
    except RouterOSError as exc:
        log.warning("RouterOSError router=%s: %s", router_id, exc)
        return f"❌ Router `{router_id}`: {exc}", cfg.command_room


# ---------------------------------------------------------------------------
# Main listen loop
# ---------------------------------------------------------------------------


def listen_loop(cfg: BotConfig) -> NoReturn:
    """Spawn matrix-commander-rs in listen mode and dispatch events forever."""
    backoff = 2

    while True:
        log.info("Starting matrix-commander-rs listener (back-off=%ds)", backoff)
        cmd = [MC_BIN, "--output", "json", "--listen", "forever"]

        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            log.critical("%s not found — is the base image correct?", MC_BIN)
            sys.exit(1)

        log.info("matrix-commander-rs PID %d", proc.pid)
        backoff = 2

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            room_id, sender, body = _parse_event(line)
            if body is None:
                continue
            log.debug("Event sender=%s room=%s body=%r", sender, room_id, body)
            response, target_room = dispatch(body, room_id, sender, cfg)
            if response:
                send_matrix_message(response, room=target_room)

        proc.wait()
        log.warning(
            "matrix-commander-rs exited code=%d — restarting in %ds",
            proc.returncode,
            backoff,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Signal handling & entry point
# ---------------------------------------------------------------------------


def _handle_sigterm(_signum: int, _frame: Any) -> NoReturn:
    log.info("SIGTERM received — shutting down")
    sys.exit(0)


def main() -> None:
    """Bot entry point."""
    signal.signal(signal.SIGTERM, _handle_sigterm)
    cfg = load_config(CONFIG_PATH)
    listen_loop(cfg)


if __name__ == "__main__":
    main()
