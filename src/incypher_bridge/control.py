"""Local Unix-socket control plane for a running bridge session."""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .session import BridgeSession

if TYPE_CHECKING:
    from .manager import SessionManager


class ControlError(RuntimeError):
    """Raised when a control request fails or the server is unavailable."""


class ControlServer:
    """Tiny JSON-lines control server bound to a private Unix socket."""

    def __init__(self, session: BridgeSession, socket_path: Path, *, manager: SessionManager | None = None) -> None:
        self.session = session
        self.socket_path = socket_path
        self.manager = manager
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> "ControlServer":
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(target=self._accept_loop, name="control", daemon=True)
        self._thread.start()
        return self

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            server = self._server
            if server is None:
                break
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_connection(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        buffer = bytearray()
        while b"\n" not in buffer and len(buffer) < 1024 * 1024:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer.extend(chunk)
        if not buffer:
            return
        line = bytes(buffer).split(b"\n", 1)[0]
        try:
            request = json.loads(line.decode("utf-8"))
        except Exception:
            response: dict[str, Any] = {"ok": False, "error": "invalid JSON request"}
        else:
            try:
                response = self._dispatch(request)
            except Exception as exc:
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = str(request.get("cmd", "")).strip().lower()
        if command == "ping":
            return {"ok": True, "message": "pong"}
        if command in ("session-create", "sessions"):
            if self.manager is None:
                return {"ok": False, "error": "use the primary run control socket for session management"}
            if command == "sessions":
                return self.manager.list_sessions()
            return self.manager.create_session(str(request.get("run_id", "")))
        if command == "status":
            return {"ok": True, "status": self.session.status()}
        if command == "events":
            since = int(request.get("since", 0) or 0)
            limit = int(request.get("limit", 200) or 200)
            from .transcript import EventLogger

            events = EventLogger.read_events(self.session.paths.events_file, since=since, limit=limit)
            return {"ok": True, "events": events}
        if command == "send":
            data: bytes
            if "data_b64" in request:
                try:
                    data = base64.b64decode(str(request["data_b64"]).encode("ascii"), validate=True)
                except Exception as exc:
                    return {"ok": False, "error": f"invalid base64 data: {exc}"}
            elif "text" in request:
                data = str(request["text"]).encode("utf-8")
            else:
                return {"ok": False, "error": "send requires data_b64 or text"}
            label = str(request.get("label", "human"))
            result = self.session.send_from_human(data, label=label)
            return result
        if command == "reconnect":
            return self.session.request_reconnect()
        if command == "stop":
            # Reply first so the caller receives an acknowledgement, then stop
            # the session asynchronously to avoid deadlocking the control path.
            threading.Thread(
                target=self.session.stop,
                args=(str(request.get("reason", "control request")),),
                daemon=True,
            ).start()
            return {"ok": True, "message": "stopping"}
        return {"ok": False, "error": f"unknown command {command!r}"}

    def shutdown(self) -> None:
        self._stop_event.set()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass


def request(socket_path: str | Path, command: str, *, timeout: float = 5.0, **fields: Any) -> dict[str, Any]:
    """Send one control request to the Unix socket and return the response."""

    path = Path(socket_path)
    if not path.exists():
        raise ControlError(f"control socket does not exist (is the bridge running?): {path}")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(path))
        payload = {"cmd": command, **fields}
        client.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        buffer = bytearray()
        while b"\n" not in buffer and len(buffer) < 1024 * 1024:
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer.extend(chunk)
    except OSError as exc:
        raise ControlError(f"control request failed: {exc}") from exc
    finally:
        try:
            client.close()
        except OSError:
            pass
    if b"\n" not in buffer:
        raise ControlError("control server returned an incomplete response")
    try:
        response = json.loads(bytes(buffer).split(b"\n", 1)[0].decode("utf-8"))
    except Exception as exc:
        raise ControlError(f"invalid control response: {exc}") from exc
    return response
