"""A maintainable local TCP session around one gated upstream connection.

The bridge connects once to the challenge service, clears the PoW gate, then
listens on ``127.0.0.1:<port>``.  Agents connect to the local endpoint as if it
were the real service; the bridge relays bytes in both directions, preserves
pending downstream bytes across agent reconnects, and exposes a small control
socket for human inspection.
"""

from __future__ import annotations

import errno
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .client import PrefixedSocket, connect
from .duration import format_clock, format_duration
from .paths import RunPaths, utc_now
from .pow import DEFAULT_VARIANTS, PowVariant
from .transcript import EventLogger, write_json_atomic


class ShutdownHook(Protocol):
    def __call__(self) -> None: ...


@dataclass
class BridgeConfig:
    host: str
    port: int
    team_key: str
    duration: float | None = 3600.0
    bind_host: str = "127.0.0.1"
    bind_port: int = 0
    auto_reconnect: bool = True
    reconnect_delay: float = 1.0
    pow_timeout: float = 120.0
    variants: Sequence[PowVariant] | None = None
    verbose: bool = False
    max_pending_bytes: int = 1024 * 1024
    name: str | None = None
    challenge_id: str = "challenge"
    run_id: str = "run"
    category: str | None = None
    challenge_name: str | None = None
    description_file: str | None = None
    description_text: str | None = None
    env_file: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BridgeError(RuntimeError):
    """Base class for bridge/session failures."""


class BridgeSession:
    """Owns one PoW-cleared upstream connection and a local relay endpoint."""

    def __init__(
        self, config: BridgeConfig, paths: RunPaths, *,
        handshake_slots: threading.BoundedSemaphore | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.logger = EventLogger(paths)

        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._handshake_slots = handshake_slots
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._reconnect_requested = threading.Event()

        self._state = "starting"
        self._started_monotonic = time.monotonic()
        self._started_utc = utc_now()
        self._deadline_monotonic: float | None = (
            time.monotonic() + config.duration if config.duration is not None else None
        )
        self._deadline_epoch: float | None = (
            time.time() + config.duration if config.duration is not None else None
        )
        self._stop_reason: str | None = None
        self._last_error: str | None = None

        self._upstream: PrefixedSocket | None = None
        self._connecting: socket.socket | None = None
        self._upstream_generation = 0
        self._pow_summary: str | None = None
        self._upstream_meta: dict[str, Any] = {}

        self._client: socket.socket | None = None
        self._client_addr: tuple[Any, ...] | None = None
        self._client_generation = 0
        self._pending = bytearray()

        self._listener: socket.socket | None = None
        self._local_host: str | None = None
        self._local_port: int | None = None

        self._threads: list[threading.Thread] = []
        self._reconnect_thread: threading.Thread | None = None
        self._shutdown_hooks: list[ShutdownHook] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self.paths.session_id

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def add_shutdown_hook(self, hook: ShutdownHook) -> None:
        self._shutdown_hooks.append(hook)

    def start(self) -> "BridgeSession":
        """Open the upstream PoW-gated connection and start the local relay."""

        self._write_meta()
        try:
            self._connect_initial()
            with self._lock:
                if self._stop_event.is_set():
                    raise BridgeError("session stopped during startup")
                self._start_listener()
                self._start_timer()
        except BaseException:
            self.stop(reason="startup failed")
            raise
        self.logger.system(
            f"agent endpoint ready at tcp://{self._local_host}:{self._local_port}"
        )
        self._write_meta()
        return self

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until the session stops; returns True if stopped."""

        return self._done_event.wait(timeout)

    def stop(self, reason: str = "user request") -> None:
        """Stop the session, closing listener, control socket, client and upstream."""

        with self._lock:
            if self._stop_event.is_set():
                return
            self._stop_event.set()
            self._stop_reason = reason
            self._state = "stopping"
        self.logger.system(f"stopping: {reason}", level="WARN")

        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        self._detach_client("bridge stopping", close=True)
        self._close_upstream("bridge stopping")
        with self._lock:
            connecting = self._connecting
        if connecting is not None:
            try:
                connecting.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connecting.close()

        for hook in list(self._shutdown_hooks):
            try:
                hook()
            except Exception:
                pass

        with self._lock:
            self._state = "stopped"
        self.logger.system("stopped", level="INFO")
        self._write_meta()
        self.logger.close()
        self._done_event.set()

    # ------------------------------------------------------------------
    # Upstream connection
    # ------------------------------------------------------------------
    def _track_connecting_socket(self, sock: socket.socket) -> None:
        with self._lock:
            if self._stop_event.is_set():
                sock.close()
                raise BridgeError("session stopped during handshake")
            self._connecting = sock

    def _open_connection(self) -> PrefixedSocket:
        slots = self._handshake_slots
        if slots is not None:
            while not slots.acquire(timeout=0.1):
                if self._stop_event.is_set():
                    raise BridgeError("session stopped while waiting for handshake capacity")
        try:
            if self._stop_event.is_set():
                raise BridgeError("session stopped before handshake")
            return connect(
                self.config.host, self.config.port, self.config.team_key,
                variants=self.config.variants or DEFAULT_VARIANTS,
                pow_timeout=self.config.pow_timeout,
                verbose=self.config.verbose, on_event=self._on_pow_event,
                cancel_event=self._stop_event, on_socket=self._track_connecting_socket,
            )
        finally:
            with self._lock:
                self._connecting = None
            if slots is not None:
                slots.release()

    def _connect_initial(self) -> None:
        with self._lock:
            if self._stop_event.is_set():
                raise BridgeError("session is stopped")
            self._state = "connecting"
        self.logger.system(
            f"connecting to {self.config.host}:{self.config.port} "
            f"and solving PoW (variants: {'auto' if not self.config.variants else len(self.config.variants)})"
        )
        try:
            sock = self._open_connection()
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
                if not self._stop_event.is_set():
                    self._state = "connect_failed"
            self.logger.system(f"initial connect failed: {exc}", level="ERROR")
            self._write_meta()
            raise BridgeError(f"could not open upstream connection: {exc}") from exc

        self._adopt_upstream(sock)

    def _adopt_upstream(self, sock: PrefixedSocket) -> None:
        with self._lock:
            if self._stop_event.is_set():
                sock.close()
                return
            self._upstream = sock
            self._upstream_generation += 1
            generation = self._upstream_generation
            self._upstream_meta = {
                "variant": getattr(sock, "variant_name", None),
                "nonce": getattr(getattr(sock, "challenge", None), "nonce_hex", None),
                "bits": getattr(getattr(sock, "challenge", None), "bits", None),
                "pow_seconds": getattr(sock, "pow_seconds", None),
                "generation": generation,
            }
            self._pow_summary = self._build_pow_summary()
            self._last_error = None
            self._state = "ready"
        self.logger.system(
            f"upstream ready (generation {generation}, {self._pow_summary or 'no PoW'})"
        )
        thread = threading.Thread(
            target=self._pump_upstream,
            args=(sock, generation),
            name=f"upstream-{generation}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def _on_pow_event(self, kind: str, payload: Any) -> None:
        if kind == "pow_banner":
            # Banner itself is noise-free enough to show as a system event.
            self.logger.system(
                "PoW banner: "
                + payload.decode("utf-8", errors="replace").strip().replace("\n", " | ")
            )
        elif kind == "pow_attempt":
            variant = payload.get("variant") if isinstance(payload, dict) else str(payload)
            self.logger.system(f"trying PoW variant {variant}")
        elif kind == "pow_solved":
            if isinstance(payload, dict):
                self.logger.system(
                    "PoW solved: variant={variant} nonce={nonce} bits={bits} in {seconds:.2f}s".format(
                        **payload
                    )
                )
        elif kind == "pow_denied":
            self.logger.system("PoW proof was denied by server", level="WARN")
        elif kind == "pow_closed":
            self.logger.system("server closed after PoW without accepting", level="WARN")
        elif kind == "pow_accepted":
            self.logger.system("PoW accepted", level="INFO")

    def _build_pow_summary(self) -> str | None:
        meta = dict(self._upstream_meta)
        if not meta.get("variant"):
            return None
        seconds = meta.get("pow_seconds")
        timing = f"{seconds:.2f}s" if isinstance(seconds, (int, float)) else "?"
        return (
            f"cleared with {meta['variant']} in {timing} "
            f"(nonce={meta.get('nonce')}, bits={meta.get('bits')})"
        )

    def _pump_upstream(self, sock: PrefixedSocket, generation: int) -> None:
        """Read from the challenge until EOF/error, then handle disconnection."""

        while not self._stop_event.is_set():
            try:
                sock.settimeout(1.0)
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop_event.is_set():
                    self.logger.system(f"upstream read error: {exc}", level="WARN")
                break
            if not data:
                break
            self.logger.record("C->A", data, source=f"upstream-{generation}")
            self._deliver_to_client(data, upstream=sock)

        self._handle_upstream_loss(sock, generation)

    def _handle_upstream_loss(self, sock: PrefixedSocket, generation: int) -> None:
        with self._lock:
            if self._upstream is not sock:
                return
            self._upstream = None
            sock.close()
            self._pending.clear()
            state_before = self._state
            self._state = "upstream_closed"
            # Signal EOF before any new upstream session can be attached.
            self._detach_client("upstream closed", close=True)
        self.logger.system(
            f"upstream generation {generation} closed"
            + (f" (was {state_before})" if state_before else ""),
            level="WARN",
        )
        self._write_meta()
        if not self._stop_event.is_set() and (
            self.config.auto_reconnect or self._reconnect_requested.is_set()
        ):
            self._ensure_reconnect_worker()

    def _ensure_reconnect_worker(self) -> None:
        with self._lock:
            if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
                return
            worker = threading.Thread(
                target=self._reconnect_worker,
                name="upstream-reconnect",
                daemon=True,
            )
            self._reconnect_thread = worker
            self._threads.append(worker)
            worker.start()

    def _reconnect_worker(self) -> None:
        try:
            self._reconnect_loop()
        finally:
            with self._lock:
                self._reconnect_thread = None
                # A newly adopted connection can reach EOF before this worker
                # exits. Its pump must not lose the request for another retry.
                if self._upstream is None and not self._stop_event.is_set() and (
                    self.config.auto_reconnect or self._reconnect_requested.is_set()
                ):
                    self._ensure_reconnect_worker()

    def _reconnect_loop(self) -> None:
        self.logger.system("reconnect worker started")
        while not self._stop_event.is_set():
            manual = self._reconnect_requested.is_set()
            if not (self.config.auto_reconnect or manual):
                break
            if not manual and self._stop_event.wait(self.config.reconnect_delay):
                break
            self._reconnect_requested.clear()
            with self._lock:
                if self._stop_event.is_set():
                    break
                self._state = "reconnecting"
            self._write_meta()
            try:
                sock = self._open_connection()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"reconnect failed: {type(exc).__name__}: {exc}"
                self.logger.system(f"reconnect failed: {exc}", level="WARN")
                # Back off a little to avoid a hot loop.
                if self._stop_event.wait(max(1.0, self.config.reconnect_delay)):
                    break
                continue
            self._adopt_upstream(sock)
            self.logger.system("reconnect succeeded")
            return
        with self._lock:
            if not self._stop_event.is_set():
                self._state = "upstream_closed"
        self._write_meta()
        self.logger.system("reconnect worker stopped", level="WARN")

    def request_reconnect(self) -> dict[str, Any]:
        with self._lock:
            if self._stop_event.is_set():
                return {"ok": False, "error": "session is stopped"}
            self._reconnect_requested.set()
            self.logger.system("manual reconnect requested")
            self._close_upstream("manual reconnect")
            self._detach_client("manual reconnect", close=True)
        if not self._stop_event.is_set():
            self._ensure_reconnect_worker()
        return {"ok": True, "state": self._state}

    def _close_upstream(self, reason: str) -> None:
        with self._lock:
            sock = self._upstream
            self._upstream = None
            self._pending.clear()
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        self.logger.system(f"closed upstream: {reason}")

    # ------------------------------------------------------------------
    # Local agent relay
    # ------------------------------------------------------------------
    def _start_listener(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.config.bind_host, self.config.bind_port))
        listener.listen(8)
        listener.settimeout(0.5)
        self._listener = listener
        self._local_host, self._local_port = listener.getsockname()[:2]
        thread = threading.Thread(target=self._accept_loop, name="local-accept", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop_event.is_set():
                    self.logger.system(f"accept failed: {exc}", level="WARN")
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            with self._lock:
                busy = self._client is not None
            if busy:
                try:
                    conn.sendall(
                        b"[incypher-bridge] busy: one agent is already attached; "
                        b"retry after it disconnects\n"
                    )
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass
                self.logger.system(f"rejected extra local client {addr!r}: busy")
                continue
            self._attach_client(conn, addr)

    def _attach_client(self, conn: socket.socket, addr: tuple[Any, ...]) -> None:
        with self._lock:
            if self._stop_event.is_set() or self._upstream is None:
                conn.close()
                return
            if self._client is not None:
                try:
                    conn.sendall(b"[incypher-bridge] busy\n")
                except OSError:
                    pass
                conn.close()
                return
            self._client = conn
            self._client_addr = addr
            self._client_generation += 1
            generation = self._client_generation
            pending = bytes(self._pending)
            self._pending.clear()
            # Replay before the pump can deliver newer bytes to this client.
            if pending:
                try:
                    conn.settimeout(1.0)
                    conn.sendall(pending)
                except OSError:
                    self._detach_client("replay failed", close=True, expected=conn)
                    return
        self.logger.system(f"agent connected from {addr!r} (client generation {generation})")
        if pending:
            self.logger.system(f"replaying {len(pending)} buffered upstream bytes to new agent")
        thread = threading.Thread(
            target=self._client_reader,
            args=(conn, generation),
            name=f"agent-{generation}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()
        self._write_meta()

    def _client_reader(self, conn: socket.socket, generation: int) -> None:
        while not self._stop_event.is_set():
            try:
                conn.settimeout(1.0)
                data = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self.logger.record("A->C", data, source=f"agent-{generation}")
            error = self._send_upstream(data, client=conn)
            if error:
                self.logger.system(f"cannot forward agent data: {error}", level="WARN")
                break
        self._detach_client(f"client generation {generation} disconnected", close=True, expected=conn)

    def _deliver_to_client(self, data: bytes, *, upstream: PrefixedSocket | None = None) -> None:
        with self._lock:
            if upstream is not None and self._upstream is not upstream:
                return
            conn = self._client
            if conn is None:
                self._append_pending_locked(data)
                return
            try:
                conn.sendall(data)
                return
            except OSError as exc:
                self.logger.system(f"agent write failed: {exc}", level="WARN")
                client = self._client
                self._client = None
                self._client_addr = None
                # Preserve bytes only within this upstream generation.
                self._append_pending_locked(data)
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    def _append_pending_locked(self, data: bytes) -> None:
        if len(data) >= self.config.max_pending_bytes:
            self._pending.clear()
            self._pending.extend(data[-self.config.max_pending_bytes :])
            self.logger.system(
                f"pending buffer exceeded {self.config.max_pending_bytes} bytes; "
                "newest bytes retained",
                level="WARN",
            )
            return
        overflow = len(self._pending) + len(data) - self.config.max_pending_bytes
        if overflow > 0:
            del self._pending[:overflow]
            self.logger.system(
                f"pending buffer overflow; dropped {overflow} oldest bytes", level="WARN"
            )
        self._pending.extend(data)

    def _detach_client(
        self, reason: str, *, close: bool, expected: socket.socket | None = None,
    ) -> None:
        with self._lock:
            if expected is not None and self._client is not expected:
                return
            conn = self._client
            addr = self._client_addr
            self._client = None
            self._client_addr = None
        if conn is None:
            return
        if close:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self.logger.system(f"agent {'closed' if close else 'detached'}: {reason}")
        self._write_meta()

    def _send_upstream(
        self, data: bytes, *, source: str = "agent", client: socket.socket | None = None,
    ) -> str | None:
        with self._write_lock:
            with self._lock:
                if client is not None and self._client is not client:
                    return "client is no longer attached"
                sock = self._upstream
            if sock is None:
                return "no upstream connection"
            try:
                sock.sendall(data)
                return None
            except OSError as exc:
                with self._lock:
                    self._last_error = f"upstream write failed: {exc}"
                self._handle_upstream_loss(sock, self._upstream_generation)
                return str(exc)

    # ------------------------------------------------------------------
    # Control / status helpers
    # ------------------------------------------------------------------
    def send_from_human(self, data: bytes, *, label: str = "human") -> dict[str, Any]:
        """Send bytes to the challenge service (human debugging / inspection)."""

        if not data:
            return {"ok": False, "error": "empty payload"}
        self.logger.record("A->C", data, source=label)
        error = self._send_upstream(data, source=label)
        if error:
            return {"ok": False, "error": error}
        return {"ok": True, "sent": len(data)}

    def status(self) -> dict[str, Any]:
        with self._lock:
            stats = self.logger.stats
            now = time.monotonic()
            uptime = now - self._started_monotonic
            remaining = (
                max(0.0, self._deadline_monotonic - now)
                if self._deadline_monotonic is not None
                else None
            )
            endpoint = (
                f"tcp://{self._local_host}:{self._local_port}"
                if self._local_port is not None
                else None
            )
            return {
                "session_id": self.session_id,
                "challenge_id": self.config.challenge_id,
                "run_id": self.config.run_id,
                "parent_run_id": self.config.metadata.get("parent_run_id"),
                "challenge_name": self.config.challenge_name,
                "category": self.config.category,
                "description_present": bool(self.config.description_text),
                "description_file": self.config.description_file,
                "description_text": self.config.description_text or "",
                "state": self._state,
                "target": f"{self.config.host}:{self.config.port}",
                "host": self.config.host,
                "port": self.config.port,
                "agent_endpoint": endpoint,
                "bind_host": self._local_host,
                "bind_port": self._local_port,
                "client_connected": self._client is not None,
                "client_addr": repr(self._client_addr) if self._client_addr else None,
                "client_generation": self._client_generation,
                "upstream_generation": self._upstream_generation,
                "pow_summary": self._pow_summary,
                "upstream_meta": dict(self._upstream_meta),
                "session_dir": str(self.paths.root),
                "challenge_dir": str(self.paths.challenge_dir),
                "challenge_md": str(self.paths.challenge_md) if self.config.description_text else None,
                "run_json": str(self.paths.run_file),
                "events_file": str(self.paths.events_file),
                "transcript_file": str(self.paths.transcript_file),
                "challenge_to_agent_file": str(self.paths.challenge_to_agent_file),
                "agent_to_challenge_file": str(self.paths.agent_to_challenge_file),
                "control_socket": str(self.paths.control_socket),
                "started_at": self._started_utc,
                "uptime_seconds": uptime,
                "remaining_seconds": remaining,
                "duration_seconds": self.config.duration,
                "duration_human": format_duration(self.config.duration),
                "remaining_human": format_clock(remaining),
                "expired": bool(self._deadline_monotonic and now >= self._deadline_monotonic),
                "stop_reason": self._stop_reason,
                "last_error": self._last_error,
                "events": stats["events"],
                "challenge_to_agent_bytes": stats["challenge_to_agent_bytes"],
                "agent_to_challenge_bytes": stats["agent_to_challenge_bytes"],
                "pending_bytes": len(self._pending),
                "auto_reconnect": self.config.auto_reconnect,
            }

    def _write_meta(self) -> None:
        with self._lock:
            self._write_meta_locked()

    def _write_meta_locked(self) -> None:
        try:
            status = self.status()
        except Exception:
            return
        status["pid"] = os.getpid()
        status["updated_at"] = utc_now()
        status["config"] = {
            "auto_reconnect": self.config.auto_reconnect,
            "reconnect_delay": self.config.reconnect_delay,
            "pow_timeout": self.config.pow_timeout,
            "env_file": self.config.env_file,
            "description_file": self.config.description_file,
            "category": self.config.category,
            "challenge_name": self.config.challenge_name,
            "metadata": self.config.metadata,
        }
        try:
            write_json_atomic(self.paths.run_file, status)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------
    def _start_timer(self) -> None:
        if self.config.duration is None:
            return
        thread = threading.Thread(target=self._timer_loop, name="duration-timer", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _timer_loop(self) -> None:
        assert self.config.duration is not None
        if self._stop_event.wait(self.config.duration):
            return
        self.stop(reason=f"time limit reached ({format_duration(self.config.duration)})")


def endpoint_for_agent(host: str, port: int) -> str:
    return f"tcp://{host}:{port}"


def wait_for_stop(session: BridgeSession) -> None:
    """Block until Ctrl-C or session stop."""

    try:
        while not session.wait(0.25):
            pass
    except KeyboardInterrupt:
        session.stop(reason="keyboard interrupt")
