"""
Meross MQTT Bridge — GUI Application (FastAPI)

Session-based web interface for configuring and monitoring the bridge.
"""

import asyncio
import functools
import hashlib
import json
import os
import secrets
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import paho.mqtt.client as mqtt_client
import yaml
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

GUI_DIR = Path(__file__).resolve().parent

# Config: prefer /app/config/config.yaml (Docker), fall back to local
_DOCKER_CONFIG = Path("/app/config/config.yaml")
_LOCAL_CONFIG = GUI_DIR.parent / "config" / "config.yaml"
CONFIG_PATH = _DOCKER_CONFIG if _DOCKER_CONFIG.exists() else _LOCAL_CONFIG

# Logs: prefer /app/logs/ (Docker), fall back to local
_DOCKER_LOGS = Path("/app/logs")
_LOCAL_LOGS = GUI_DIR.parent / "logs"
LOGS_DIR = _DOCKER_LOGS if _DOCKER_LOGS.is_dir() else _LOCAL_LOGS

TEMPLATES_DIR = GUI_DIR / "templates"

# ---------------------------------------------------------------------------
# Config helpers (thread-safe, always fresh from disk)
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()


def read_config() -> dict:
    """Read config.yaml from disk. Returns empty dict on error."""
    with _config_lock:
        try:
            with open(CONFIG_PATH, "r") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}


def write_config(cfg: dict) -> None:
    """Write full config dict to config.yaml, preserving key order."""
    with _config_lock:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def update_config_section(section: str, data) -> None:
    """Update a single top-level section in config.yaml without touching others."""
    cfg = read_config()
    cfg[section] = data
    write_config(cfg)

# ---------------------------------------------------------------------------
# Session / Auth
# ---------------------------------------------------------------------------

# Session secret: auto-generate on first boot and persist to config dir
_SECRET_FILE = Path("/app/config/.secret_key") if Path("/app/config").is_dir() else (
    Path(__file__).resolve().parent.parent / "config" / ".secret_key"
)


def _load_or_create_secret() -> str:
    """Load secret key from file, or generate and persist a new one."""
    env_key = os.environ.get("GUI_SECRET_KEY")
    if env_key:
        return env_key
    try:
        if _SECRET_FILE.exists():
            return _SECRET_FILE.read_text().strip()
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(key)
        _SECRET_FILE.chmod(0o600)
    except OSError:
        pass  # If we can't persist, we still run (sessions reset on restart)
    return key


SECRET_KEY = _load_or_create_secret()
SESSION_MAX_AGE = 8 * 60 * 60  # 8 hours
serializer = URLSafeTimedSerializer(SECRET_KEY)

# In-memory flash messages keyed by session token
_flash_messages: dict[str, list[str]] = {}

# ---------------------------------------------------------------------------
# Login rate limiting (in-memory)
# ---------------------------------------------------------------------------
_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 30


def _is_rate_limited(ip: str) -> bool:
    """Check if an IP is rate-limited. Prune old entries."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Prune attempts older than lockout window
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_LOCKOUT_SECONDS]
    return len(_login_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------

def _generate_csrf_token(session_token: str) -> str:
    """Generate a CSRF token tied to the session."""
    return hashlib.sha256(f"{SECRET_KEY}:{session_token}:csrf".encode()).hexdigest()[:32]


def _verify_csrf_token(session_token: str, csrf_token: str) -> bool:
    """Verify a CSRF token matches the session."""
    expected = _generate_csrf_token(session_token)
    return secrets.compare_digest(expected, csrf_token)


def create_session_token(username: str) -> str:
    return serializer.dumps({"user": username})


def verify_session_token(token: str) -> Optional[dict]:
    try:
        return serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def flash(token: str, message: str) -> None:
    _flash_messages.setdefault(token, []).append(message)


def get_flashed(token: str) -> list[str]:
    return _flash_messages.pop(token, [])

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def get_current_user(session: Optional[str] = Cookie(None)) -> Optional[str]:
    """Return username from session cookie, or None."""
    if not session:
        return None
    data = verify_session_token(session)
    if data and "user" in data:
        return data["user"]
    return None


def require_auth(request: Request) -> str:
    """Dependency that enforces authentication. Returns username."""
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    user = get_current_user(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return user

# ---------------------------------------------------------------------------
# Startup: ensure password hash exists
# ---------------------------------------------------------------------------


def ensure_default_password():
    """If gui.password_hash is empty, generate bcrypt hash of 'admin' and write it."""
    cfg = read_config()
    gui = cfg.get("gui", {})
    if not gui.get("password_hash"):
        hashed = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode("utf-8")
        gui["password_hash"] = hashed
        cfg["gui"] = gui
        write_config(cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_default_password()
    yield

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Serve static files if directory exists
_static_dir = GUI_DIR / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# ---------------------------------------------------------------------------
# Middleware: force password change redirect
# ---------------------------------------------------------------------------


@app.middleware("http")
async def force_password_change_middleware(request: Request, call_next):
    # Skip for non-authenticated or specific paths
    skip_paths = {"/login", "/logout", "/change-password"}
    if request.url.path in skip_paths or request.url.path.startswith("/static"):
        return await call_next(request)

    token = request.cookies.get("session")
    if token and verify_session_token(token):
        cfg = read_config()
        gui = cfg.get("gui", {})
        if gui.get("force_password_change", False):
            return RedirectResponse(url="/change-password", status_code=303)

    return await call_next(request)

# ---------------------------------------------------------------------------
# Template helper
# ---------------------------------------------------------------------------


def render(
    request: Request,
    template: str,
    context: dict | None = None,
    user: str = "",
    token: str = "",
) -> HTMLResponse:
    ctx = {"request": request, "current_user": user}
    if token:
        ctx["messages"] = get_flashed(token)
        ctx["csrf_token"] = _generate_csrf_token(token)
    else:
        ctx["messages"] = []
        ctx["csrf_token"] = ""
    if context:
        ctx.update(context)
    return templates.TemplateResponse(template, ctx)

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    client_ip = request.client.host if request.client else "unknown"

    if _is_rate_limited(client_ip):
        return render(request, "login.html", {"error": "Too many login attempts. Please wait 30 seconds."})

    cfg = read_config()
    gui = cfg.get("gui", {})
    stored_user = gui.get("username", "admin")
    stored_hash = gui.get("password_hash", "")

    if username != stored_user or not stored_hash:
        _record_login_attempt(client_ip)
        return render(request, "login.html", {"error": "Invalid credentials"})

    if not bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8")):
        _record_login_attempt(client_ip)
        return render(request, "login.html", {"error": "Invalid credentials"})

    token = create_session_token(username)
    redirect_url = "/change-password" if gui.get("force_password_change", False) else "/dashboard"
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.set_cookie(
        key="session",
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    return response

# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    cfg = read_config()
    forced = cfg.get("gui", {}).get("force_password_change", False)
    return render(request, "change_password.html", {"forced": forced}, user=user, token=token)


@app.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str = Form(""),
    user: str = Depends(require_auth),
):
    token = request.cookies.get("session", "")

    # CSRF check
    if not token or not _verify_csrf_token(token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    cfg = read_config()
    gui = cfg.get("gui", {})
    stored_hash = gui.get("password_hash", "")

    if not bcrypt.checkpw(current_password.encode("utf-8"), stored_hash.encode("utf-8")):
        return render(
            request, "change_password.html",
            {"error": "Current password is incorrect", "forced": gui.get("force_password_change", False)},
            user=user, token=token,
        )

    if new_password != confirm_password:
        return render(
            request, "change_password.html",
            {"error": "New passwords do not match", "forced": gui.get("force_password_change", False)},
            user=user, token=token,
        )

    if len(new_password) < 8:
        return render(
            request, "change_password.html",
            {"error": "Password must be at least 8 characters", "forced": gui.get("force_password_change", False)},
            user=user, token=token,
        )

    new_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    gui["password_hash"] = new_hash
    gui["force_password_change"] = False
    cfg["gui"] = gui
    write_config(cfg)

    flash(token, "Password changed successfully.")
    return RedirectResponse(url="/dashboard", status_code=303)

# ---------------------------------------------------------------------------
# Account settings
# ---------------------------------------------------------------------------

import re

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    return render(request, "account.html", user=user, token=token)


@app.post("/account/username")
async def account_change_username(
    request: Request,
    current_password: str = Form(...),
    new_username: str = Form(...),
    csrf_token: str = Form(""),
    user: str = Depends(require_auth),
):
    token = request.cookies.get("session", "")

    if not token or not _verify_csrf_token(token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    cfg = read_config()
    gui = cfg.get("gui", {})
    stored_hash = gui.get("password_hash", "")

    if not bcrypt.checkpw(current_password.encode("utf-8"), stored_hash.encode("utf-8")):
        return render(
            request, "account.html",
            {"username_error": "Current password is incorrect"},
            user=user, token=token,
        )

    if len(new_username) < 3:
        return render(
            request, "account.html",
            {"username_error": "Username must be at least 3 characters"},
            user=user, token=token,
        )

    if not _USERNAME_RE.match(new_username):
        return render(
            request, "account.html",
            {"username_error": "Username can only contain letters, numbers, and underscores"},
            user=user, token=token,
        )

    gui["username"] = new_username
    cfg["gui"] = gui
    write_config(cfg)

    # Invalidate session — force re-login with new username
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    flash(token, "Username changed. Please log in again.")
    return response


@app.post("/account/password")
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str = Form(""),
    user: str = Depends(require_auth),
):
    token = request.cookies.get("session", "")

    if not token or not _verify_csrf_token(token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    cfg = read_config()
    gui = cfg.get("gui", {})
    stored_hash = gui.get("password_hash", "")

    if not bcrypt.checkpw(current_password.encode("utf-8"), stored_hash.encode("utf-8")):
        return render(
            request, "account.html",
            {"password_error": "Current password is incorrect"},
            user=user, token=token,
        )

    if new_password != confirm_password:
        return render(
            request, "account.html",
            {"password_error": "New passwords do not match"},
            user=user, token=token,
        )

    if len(new_password) < 8:
        return render(
            request, "account.html",
            {"password_error": "Password must be at least 8 characters"},
            user=user, token=token,
        )

    new_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    gui["password_hash"] = new_hash
    cfg["gui"] = gui
    write_config(cfg)

    flash(token, "Password updated.")
    return RedirectResponse(url="/account", status_code=303)

# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    return render(request, "dashboard.html", user=user, token=token)


@app.get("/doors", response_class=HTMLResponse)
async def doors_page(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    cfg = read_config()
    doors = cfg.get("doors", [])
    return render(request, "doors.html", {"doors": doors}, user=user, token=token)


@app.post("/doors/save")
async def doors_save(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    body = await request.json()
    doors = body.get("doors", [])

    update_config_section("doors", doors)

    # Touch config file to signal the bridge to reload
    try:
        CONFIG_PATH.touch()
    except OSError:
        pass

    flash(token, "Door configuration saved.")
    return JSONResponse({"success": True, "message": "Door configuration saved."})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    cfg = read_config()
    meross = cfg.get("meross", {})
    mqtt_cfg = cfg.get("mqtt", {})
    bridge = cfg.get("bridge", {})
    return render(
        request, "settings.html",
        {"meross": meross, "mqtt": mqtt_cfg, "bridge": bridge},
        user=user, token=token,
    )


@app.post("/settings/save")
async def settings_save(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    body = await request.json()

    cfg = read_config()
    if "meross" in body:
        incoming_meross = body["meross"]
        # Preserve existing password if not provided
        if not incoming_meross.get("password"):
            incoming_meross["password"] = cfg.get("meross", {}).get("password", "")
        cfg["meross"] = incoming_meross
    if "mqtt" in body:
        incoming_mqtt = body["mqtt"]
        # Preserve existing password if not provided
        if not incoming_mqtt.get("pass"):
            incoming_mqtt["pass"] = cfg.get("mqtt", {}).get("pass", "")
        cfg["mqtt"] = incoming_mqtt
    if "bridge" in body:
        cfg["bridge"] = body["bridge"]
    write_config(cfg)

    flash(token, "Settings saved.")
    return JSONResponse({"success": True, "message": "Settings saved."})


@app.post("/settings/test-meross")
async def test_meross(request: Request, user: str = Depends(require_auth)):
    body = await request.json()
    email = body.get("email", "")
    password = body.get("password", "")
    api_url = body.get("api_url", "https://iot.meross.com")

    # If password field was left empty, use the saved one
    if not password:
        cfg = read_config()
        password = cfg.get("meross", {}).get("password", "")

    if not email or not password:
        return JSONResponse({"success": False, "message": "Email and password are required."})

    try:
        from meross_iot.http_api import MerossHttpClient

        client = await MerossHttpClient.async_from_user_password(
            email=email, password=password, api_base_url=api_url,
        )
        await client.async_logout()
        return JSONResponse({"success": True, "message": "Connection successful."})
    except ImportError:
        return JSONResponse({"success": False, "message": "meross_iot library not installed."})
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Connection failed: {e}"})


@app.post("/settings/test-mqtt")
async def test_mqtt(request: Request, user: str = Depends(require_auth)):
    body = await request.json()
    host = body.get("host", "")
    port = int(body.get("port", 1883))
    mqtt_user = body.get("user", "")
    mqtt_pass = body.get("pass", "")

    # If password field was left empty, use the saved one
    if not mqtt_pass:
        cfg = read_config()
        mqtt_pass = cfg.get("mqtt", {}).get("pass", "")

    if not host:
        return JSONResponse({"success": False, "message": "Host is required."})

    def _try_connect():
        client = mqtt_client.Client(client_id=f"gui-test-{int(time.time())}")
        if mqtt_user:
            client.username_pw_set(mqtt_user, mqtt_pass)
        client.connect(host, port, keepalive=5)
        client.disconnect()
        return f"Connected to broker at {host}:{port}"

    try:
        loop = asyncio.get_event_loop()
        msg = await asyncio.wait_for(
            loop.run_in_executor(None, _try_connect),
            timeout=5.0,
        )
        return JSONResponse({"success": True, "message": msg})
    except asyncio.TimeoutError:
        return JSONResponse({"success": False, "message": f"Connection to {host}:{port} timed out."})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

# ---------------------------------------------------------------------------
# Logs page
# ---------------------------------------------------------------------------


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user: str = Depends(require_auth)):
    token = request.cookies.get("session", "")
    return render(request, "logs.html", user=user, token=token)

# ---------------------------------------------------------------------------
# WebSocket: tail logs/bridge.log
# ---------------------------------------------------------------------------


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket, session: Optional[str] = Cookie(None)):
    # Authenticate WebSocket
    if not session or not verify_session_token(session):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    log_file = LOGS_DIR / "bridge.log"

    try:
        # Send last 100 lines on connect
        if log_file.exists():
            with open(log_file, "r") as f:
                lines = f.readlines()
            for line in lines[-100:]:
                await websocket.send_text(line.rstrip("\n"))
            last_pos = log_file.stat().st_size
        else:
            await websocket.send_text("[Log file not found — waiting for bridge to start...]")
            last_pos = 0

        # Tail loop
        while True:
            await asyncio.sleep(1)
            try:
                if not log_file.exists():
                    last_pos = 0
                    continue
                current_size = log_file.stat().st_size
                if current_size < last_pos:
                    # File was truncated/rotated
                    last_pos = 0
                if current_size > last_pos:
                    with open(log_file, "r") as f:
                        f.seek(last_pos)
                        new_lines = f.readlines()
                    last_pos = current_size
                    for line in new_lines:
                        await websocket.send_text(line.rstrip("\n"))
            except OSError:
                continue

    except WebSocketDisconnect:
        pass

# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def api_status(user: str = Depends(require_auth)):
    log_file = LOGS_DIR / "bridge.log"
    bridge_running = False
    try:
        if log_file.exists():
            mtime = log_file.stat().st_mtime
            bridge_running = (time.time() - mtime) < 60
    except OSError:
        pass
    return {"bridge_running": bridge_running}


@app.get("/api/doors/state")
async def api_doors_state(user: str = Depends(require_auth)):
    cfg = read_config()
    config_doors = cfg.get("doors", [])

    # Load state file if it exists
    state_file = LOGS_DIR / "door_states.json"
    state_by_channel: dict = {}
    last_updated = None
    try:
        if state_file.exists():
            raw = json.loads(state_file.read_text())
            last_updated = raw.get("timestamp")
            for key, val in raw.items():
                if key == "timestamp":
                    continue
                if isinstance(val, dict) and "channel" in val:
                    state_by_channel[val["channel"]] = val
    except (OSError, json.JSONDecodeError):
        pass

    # Build normalized array — one entry per configured door
    result = []
    for door in config_doors:
        ch = door.get("channel")
        saved = state_by_channel.get(ch, {})
        result.append({
            "channel": ch,
            "name": door.get("name") or f"Door {ch}",
            "enabled": door.get("enabled", False),
            "state": saved.get("state", "unknown"),
            "command_topic": door.get("command_topic", ""),
            "state_topic": door.get("state_topic", ""),
            "last_updated": last_updated,
        })

    return result


@app.post("/api/door/{channel}/command")
async def api_door_command(channel: int, request: Request, user: str = Depends(require_auth)):
    body = await request.json()
    command = body.get("command", "").lower()
    if command not in ("open", "close"):
        return JSONResponse({"success": False, "message": "Command must be 'open' or 'close'."}, status_code=400)

    cfg = read_config()
    doors = cfg.get("doors", [])
    door = next((d for d in doors if d.get("channel") == channel), None)
    if not door:
        return JSONResponse({"success": False, "message": f"Door with channel {channel} not found."}, status_code=404)

    mqtt_cfg = cfg.get("mqtt", {})
    host = mqtt_cfg.get("host", "")
    port = mqtt_cfg.get("port", 1883)
    mqtt_user = mqtt_cfg.get("user", "")
    mqtt_pass = mqtt_cfg.get("pass", "")

    if not host:
        return JSONResponse({"success": False, "message": "MQTT host not configured."}, status_code=500)

    command_topic = door.get("command_topic", "")
    if not command_topic:
        return JSONResponse({"success": False, "message": "No command topic configured for this door."}, status_code=400)

    try:
        client = mqtt_client.Client(client_id=f"gui-cmd-{channel}-{int(time.time())}")
        if mqtt_user:
            client.username_pw_set(mqtt_user, mqtt_pass)
        client.connect(host, int(port), keepalive=10)
        client.publish(command_topic, command, qos=1)
        client.disconnect()
        return {"success": True, "message": f"Sent '{command}' to {command_topic}"}
    except Exception as e:
        return JSONResponse({"success": False, "message": f"MQTT publish failed: {e}"}, status_code=500)
