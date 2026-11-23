#!/usr/bin/env python3
"""
token-guard hook_daemon: Zero-dependency persistent background worker keeping
read_guard, cmd_filter, search_guard, and outline_gen hot in memory over a
local loopback socket (127.0.0.1 with TCP_NODELAY) for <1.5ms request latency.
Auto-terminates after 30 minutes of idle inactivity.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
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


_PORT_CACHE: dict[str, int] = {}
_PERSISTENT_SOCK: dict[int, socket.socket] = {}


def _get_daemon_port(state_dir: Path | None = None) -> int | None:
    meta_file = daemon_meta_path(state_dir)
    key = str(meta_file)
    if key in _PORT_CACHE:
        return _PORT_CACHE[key]
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        port = int(meta["port"])
        _PORT_CACHE[key] = port
        return port
    except Exception:
        return None


def query_daemon(payload: dict[str, Any], state_dir: Path | None = None, timeout: float = 0.08) -> dict[str, Any] | None:
    """Send a hook payload to the running daemon over a persistent TCP_NODELAY socket (<1.5ms)."""
    port = _get_daemon_port(state_dir)
    if port is None:
        return None

    for attempt in range(2):
        sock = _PERSISTENT_SOCK.get(port)
        try:
            if sock is None:
                sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _PERSISTENT_SOCK[port] = sock

            msg = (json.dumps(payload) + "\n").encode("utf-8")
            sock.sendall(msg)
            chunks: list[bytes] = []
            while True:
                data = sock.recv(65536)
                if not data:
                    raise ConnectionResetError()
                chunks.append(data)
                if b"\n" in data:
                    break
            if isinstance(payload, dict) and payload.get("__shutdown__"):
                sock.close()
                _PERSISTENT_SOCK.pop(port, None)
                _PORT_CACHE.clear()
            raw_resp = b"".join(chunks).decode("utf-8", errors="replace").strip()
            if not raw_resp:
                return None
            return json.loads(raw_resp)
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            _PERSISTENT_SOCK.pop(port, None)
            if attempt == 1:
                _PORT_CACHE.clear()
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
        "cwd": str(s_dir),
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


def serve_forever(state_dir: Path, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    meta_file = daemon_meta_path(state_dir)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(32)
    srv.settimeout(1.0)
    port = srv.getsockname()[1]

    meta_file.write_text(
        json.dumps({"pid": os.getpid(), "port": port, "started_at": time.time()}),
        encoding="utf-8",
    )

    last_active = time.time()
    try:
        while time.time() - last_active < idle_timeout:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            last_active = time.time()
            with conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(5.0)
                pending = b""
                shutdown_requested = False
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    last_active = time.time()
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
                        if isinstance(req, dict) and req.get("__ping__"):
                            conn.sendall(b'{"pong": true}\n')
                            continue
                        if isinstance(req, dict) and req.get("__shutdown__"):
                            conn.sendall(b'{"shutdown": true}\n')
                            shutdown_requested = True
                            break
                        try:
                            resp = handle_payload(req)
                        except Exception:
                            resp = {"decision": "allow"}
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    if shutdown_requested:
                        break
                if shutdown_requested:
                    break
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
