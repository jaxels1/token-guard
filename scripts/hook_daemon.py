#!/usr/bin/env python3
"""
token-guard hook_daemon: Zero-dependency persistent background worker keeping
read_guard, cmd_filter, search_guard, and outline_gen hot in memory over a
local loopback socket (127.0.0.1 with TCP_NODELAY) for <1.5ms request latency.
Auto-terminates after 30 minutes of idle inactivity.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from hook_entry import handle_payload  # noqa: E402
from read_guard import default_state_dir  # noqa: E402

IDLE_TIMEOUT_SECONDS = 1800.0


def daemon_meta_path(state_dir: Path | None = None) -> Path:
    s_dir = state_dir or default_state_dir()
    return s_dir / "daemon.json"


_META_LOCK = threading.Lock()
_META_CACHE: dict[str, tuple[int, str]] = {}
_THREAD_LOCAL = threading.local()


def _get_thread_socks() -> dict[int, socket.socket]:
    socks = getattr(_THREAD_LOCAL, "socks", None)
    if socks is None:
        socks = {}
        _THREAD_LOCAL.socks = socks
    return socks


def close_client_sockets() -> None:
    """Close any thread-local persistent daemon sockets held by the calling thread."""
    socks = getattr(_THREAD_LOCAL, "socks", None)
    if not socks:
        return
    for sock in list(socks.values()):
        try:
            sock.close()
        except Exception:
            pass
    socks.clear()


def _get_daemon_meta(state_dir: Path | None = None, *, force_refresh: bool = False) -> tuple[int, str] | None:
    meta_file = daemon_meta_path(state_dir)
    key = str(meta_file)
    with _META_LOCK:
        if not force_refresh and key in _META_CACHE:
            return _META_CACHE[key]
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        port = int(meta["port"])
        token = str(meta.get("token") or "")
        with _META_LOCK:
            _META_CACHE[key] = (port, token)
        return port, token
    except Exception:
        return None


def _clear_daemon_cache(state_dir: Path | None = None) -> None:
    key = str(daemon_meta_path(state_dir))
    with _META_LOCK:
        _META_CACHE.pop(key, None)


def query_daemon(payload: dict[str, Any], state_dir: Path | None = None, timeout: float = 0.08) -> dict[str, Any] | None:
    """Send a hook payload to the running daemon over a per-thread TCP_NODELAY socket (<1.5ms)."""
    meta = _get_daemon_meta(state_dir)
    if meta is None:
        return None

    socks = _get_thread_socks()
    for attempt in range(2):
        if attempt == 1:
            meta = _get_daemon_meta(state_dir, force_refresh=True)
            if meta is None:
                return None
        port, token = meta
        sock = socks.get(port)
        try:
            if sock is None:
                sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                socks[port] = sock

            if isinstance(payload, dict) and (payload.get("__ping__") or payload.get("__shutdown__")):
                wire: dict[str, Any] = {**payload, "__token__": token}
            else:
                wire = {"__token__": token, "payload": payload}
            msg = (json.dumps(wire) + "\n").encode("utf-8")
            sock.sendall(msg)
            chunks: list[bytes] = []
            while True:
                data = sock.recv(65536)
                if not data:
                    raise ConnectionResetError()
                chunks.append(data)
                if b"\n" in data:
                    break
            raw_resp = b"".join(chunks).decode("utf-8", errors="replace").strip()
            if not raw_resp:
                return None
            resp = json.loads(raw_resp)
            if isinstance(resp, dict) and resp.get("error") == "unauthorized":
                sock.close()
                socks.pop(port, None)
                _clear_daemon_cache(state_dir)
                continue
            if isinstance(payload, dict) and payload.get("__shutdown__"):
                sock.close()
                socks.pop(port, None)
                _clear_daemon_cache(state_dir)
            return resp
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            socks.pop(port, None)
            if attempt == 1:
                _clear_daemon_cache(state_dir)
                return None
    return None


def ensure_daemon_started(state_dir: Path | None = None) -> bool:
    """Spawn the background daemon if not already reachable."""
    if os.environ.get("TOKEN_GUARD_NO_DAEMON") == "1":
        return False
    ping = query_daemon({"__ping__": True}, state_dir=state_dir, timeout=0.05)
    if ping and ping.get("pong"):
        return True

    s_dir = state_dir or default_state_dir()
    s_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": tempfile.gettempdir() if "tempfile" in globals() else os.path.dirname(os.path.abspath(__file__)),
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve", str(s_dir)],
            **kwargs,
        )
        return True
    except Exception:
        return False


def _handle_client(
    conn: socket.socket,
    auth_token: str,
    last_active: list[float],
    shutdown_event: threading.Event,
) -> None:
    with conn:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(2.0)
        except OSError:
            return
        pending = b""
        while not shutdown_event.is_set():
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            last_active[0] = time.time()
            pending += chunk
            while b"\n" in pending:
                line_bytes, pending = pending.split(b"\n", 1)
                raw = line_bytes.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    conn.sendall(b'{"decision": "allow"}\n')
                    continue
                if not isinstance(req, dict):
                    conn.sendall(b'{"error": "unauthorized"}\n')
                    return
                req_token = req.get("__token__")
                if not isinstance(req_token, str) or not hmac.compare_digest(req_token, auth_token):
                    conn.sendall(b'{"error": "unauthorized"}\n')
                    return
                if req.get("__ping__"):
                    conn.sendall(b'{"pong": true}\n')
                    continue
                if req.get("__shutdown__"):
                    conn.sendall(b'{"shutdown": true}\n')
                    shutdown_event.set()
                    return
                payload = req.get("payload") if isinstance(req.get("payload"), dict) else req
                try:
                    resp = handle_payload(payload)
                except Exception:
                    resp = {"decision": "allow"}
                try:
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except OSError:
                    return


def serve_forever(state_dir: Path, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    meta_file = daemon_meta_path(state_dir)
    auth_token = secrets.token_hex(16)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.settimeout(0.5)
    port = srv.getsockname()[1]

    tmp_meta = meta_file.with_name(f"{meta_file.name}.{os.getpid()}.tmp")
    tmp_meta.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "token": auth_token,
                "started_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    os.replace(tmp_meta, meta_file)
    try:
        os.chmod(meta_file, 0o600)
    except OSError:
        pass

    last_active = [time.time()]
    shutdown_event = threading.Event()
    try:
        while not shutdown_event.is_set() and (time.time() - last_active[0] < idle_timeout):
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            last_active[0] = time.time()
            t = threading.Thread(
                target=_handle_client,
                args=(conn, auth_token, last_active, shutdown_event),
                daemon=True,
            )
            t.start()
    finally:
        try:
            srv.close()
        except Exception:
            pass
        try:
            if meta_file.is_file():
                meta_file.unlink()
        except Exception:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) >= 1 and args[0] == "--serve":
        s_dir = Path(args[1]) if len(args) >= 2 else default_state_dir()
        return serve_forever(s_dir)
    if "--stop" in args:
        query_daemon({"__shutdown__": True}, timeout=0.2)
        return 0
    ensure_daemon_started()
    return 0


if __name__ == "__main__":
    sys.exit(main())
