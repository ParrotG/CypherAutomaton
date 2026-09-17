"""Manage independent persistent TCP sessions for one challenge target."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from .paths import RunPaths, safe_component
from .session import BridgeError, BridgeSession


class SessionManager:
    """Bound session count and handshakes without sharing protocol streams.

    The primary run owns this manager. Children have their own run directories,
    control sockets and relay endpoints, so existing run selectors still work.
    Creation is asynchronous and idempotent by child run id within this manager.
    """

    def __init__(
        self, primary: BridgeSession, *, max_sessions: int = 4,
        max_handshakes: int = 1,
    ) -> None:
        if max_sessions < 1 or max_handshakes < 1:
            raise ValueError("session and handshake limits must be positive")
        self.primary = primary
        self.max_sessions = max_sessions
        self.max_handshakes = max_handshakes
        self._lock = threading.RLock()
        self._stopped = False
        self._sessions = {primary.config.run_id: primary}
        self._workers: list[threading.Thread] = []
        self._slots = threading.BoundedSemaphore(max_handshakes)
        primary._handshake_slots = self._slots
        primary.add_shutdown_hook(self.shutdown)

    def list_sessions(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "max_sessions": self.max_sessions,
                "max_handshakes": self.max_handshakes,
                "sessions": [session.status() for session in self._sessions.values()],
            }

    def create_session(self, run_id: str) -> dict[str, Any]:
        from .control import ControlServer

        if not run_id or safe_component(run_id) != run_id:
            raise ValueError("run_id must be a safe identifier of at most 64 characters")
        with self._lock:
            if self._stopped or self.primary.stopped:
                raise BridgeError("session manager is stopped")
            existing = self._sessions.get(run_id)
            if existing is not None:
                return {"ok": True, "created": False, "status": existing.status()}
            if sum(not session.stopped for session in self._sessions.values()) >= self.max_sessions:
                raise BridgeError("session capacity reached")
            # Reserve exclusively to prevent another manager from reusing logs.
            paths = RunPaths.create(
                self.primary.paths.state_dir,
                challenge_id=self.primary.config.challenge_id, run_id=run_id, exclusive=True,
            )
            remaining = self.primary.status()["remaining_seconds"]
            config = replace(
                self.primary.config, run_id=run_id, bind_port=0, duration=remaining,
                metadata={**self.primary.config.metadata,
                          "parent_run_id": self.primary.config.run_id},
            )
            session = BridgeSession(config, paths, handshake_slots=self._slots)
            control = ControlServer(session, paths.control_socket)
            session.add_shutdown_hook(control.shutdown)
            self._sessions[run_id] = session
            try:
                control.start()
                session._write_meta()
                worker = threading.Thread(
                    target=self._start_session, args=(session,),
                    name=f"session-start-{run_id}", daemon=True,
                )
                self._workers.append(worker)
                worker.start()
            except Exception:
                session.stop("session creation failed")
                raise
            return {"ok": True, "created": True, "status": session.status()}

    @staticmethod
    def _start_session(session: BridgeSession) -> None:
        try:
            session.start()
        except Exception:
            # BridgeSession records the failure and releases its resources.
            session.stop("session startup failed")

    def shutdown(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            children = [s for s in self._sessions.values() if s is not self.primary]
        for session in children:
            session.stop("parent bridge stopped")
