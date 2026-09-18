"""Asyncio connector: one stable local TCP endpoint per logical connection slot."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .models import ConnectorCreate
from .pow import PowError, parse_pow_challenge, solve_pow_async
from .store import ConnectorStore, utc_now

_BANNER_RE = re.compile(rb"nonce=[0-9a-fA-F]+\s+bits=\d+")
_READY = "ready"


class ConnectorError(RuntimeError):
    """Raised when a connector cannot be created or operated."""


class Connector:
    """A stable local endpoint backed by replaceable upstream generations.

    The local TCP listener survives upstream loss and reconnect.  A connected
    agent sees EOF when the current upstream generation dies; it can reconnect
    to the same endpoint and continue as a new generation becomes ready.
    """

    def __init__(
        self,
        spec: ConnectorCreate,
        *,
        host: str,
        port: int,
        team_key: str,
        state_root: Path,
        handshake_semaphore: asyncio.Semaphore,
    ) -> None:
        self.spec = spec
        self.connector_id = spec.connector_id or f"c-{uuid.uuid4().hex[:10]}"
        self._host = host
        self._port = port
        self._team_key = team_key
        self._state_root = Path(state_root)
        self._handshake_semaphore = handshake_semaphore

        self._server: asyncio.AbstractServer | None = None
        self._upstream_reader: asyncio.StreamReader | None = None
        self._upstream_writer: asyncio.StreamWriter | None = None
        self._agent_reader: asyncio.StreamReader | None = None
        self._agent_writer: asyncio.StreamWriter | None = None
        self._upstream_task: asyncio.Task[None] | None = None

        self._lock = asyncio.Lock()
        self._state = "created"
        self._generation = 0
        self._pending = bytearray()
        self._last_error: str | None = None
        self._pow_summary: str | None = None
        self._force_reconnect = False
        self._stopping = False
        self._created_at = utc_now()
        self._updated_at = self._created_at

        self._directory = self._state_root / self.connector_id
        self._store = ConnectorStore(self._directory)

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> "Connector":
        if self._host not in ("127.0.0.1", "localhost", "::1"):
            raise ConnectorError("bridge only binds loopback addresses")
        self._server = await asyncio.start_server(
            self._handle_agent,
            self._host,
            self._port,
        )
        socket = self._server.sockets[0]
        self._port = int(socket.getsockname()[1])
        self._set_state("connecting")
        self._store.system(
            f"connector {self.connector_id} listening on {self._host}:{self._port}"
        )
        self._store.system(f"target={self.spec.target!r}")
        self._upstream_task = asyncio.create_task(
            self._upstream_loop(), name=f"connector-{self.connector_id}-upstream"
        )
        self._write_meta()
        return self

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._force_reconnect = False
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        await self._close_agent()
        await self._close_upstream()
        task = self._upstream_task
        self._upstream_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._set_state("closed")
        self._store.system("connector stopped")
        self._write_meta()
        self._store.close()

    async def request_reconnect(self) -> None:
        async with self._lock:
            self._force_reconnect = True
            writer = self._upstream_writer
        self._store.system("manual reconnect requested")
        if writer is not None:
            writer.close()
        task = self._upstream_task
        if task is None or task.done():
            if not self._stopping:
                self._upstream_task = asyncio.create_task(
                    self._upstream_loop(),
                    name=f"connector-{self.connector_id}-upstream",
                )

    # ------------------------------------------------------------------
    # Info / persistence
    # ------------------------------------------------------------------
    def _info(self) -> dict[str, Any]:
        endpoint = f"tcp://{self._host}:{self._port}" if self._port else None
        return {
            "connector_id": self.connector_id,
            "target": self.spec.target,
            "endpoint": endpoint,
            "bind_host": self._host,
            "bind_port": self._port or None,
            "state": self._state,
            "generation": self._generation,
            "client_connected": self._agent_writer is not None,
            "pending_bytes": len(self._pending),
            "pow_summary": self._pow_summary,
            "last_error": self._last_error,
            "auto_reconnect": self.spec.auto_reconnect,
            "metadata": dict(self.spec.metadata),
            "created_at": self._created_at,
            "updated_at": self._updated_at,
            "directory": str(self._directory),
        }

    def info(self) -> dict[str, Any]:
        return self._info()

    def read_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._store.read_events(limit=limit)

    def _set_state(self, state: str) -> None:
        self._state = state
        self._updated_at = utc_now()
        self._write_meta()

    def _write_meta(self) -> None:
        self._store.write_meta(self._info())

    # ------------------------------------------------------------------
    # Upstream lifecycle
    # ------------------------------------------------------------------
    async def _upstream_loop(self) -> None:
        while not self._stopping:
            self._set_state("connecting")
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._store.system(f"upstream connect failed: {exc}", level="WARN")
                await self._close_upstream()
                if self._stopping:
                    break
                if self.spec.auto_reconnect or self._force_reconnect:
                    self._force_reconnect = False
                    self._set_state("reconnecting")
                    await asyncio.sleep(self.spec.reconnect_delay)
                    continue
                self._set_state("upstream_closed")
                return

            try:
                await self._read_upstream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._store.system(f"upstream lost: {exc}", level="WARN")
            finally:
                await self._close_agent()
                await self._close_upstream()
                if not self._stopping:
                    self._pending.clear()
                    self._pow_summary = None
                    self._store.system("upstream generation closed", level="WARN")

            if self._stopping:
                return
            if self.spec.auto_reconnect or self._force_reconnect:
                self._force_reconnect = False
                self._set_state("reconnecting")
                await asyncio.sleep(self.spec.reconnect_delay)
                continue
            self._set_state("upstream_closed")
            return

    async def _connect_once(self) -> None:
        async with self._handshake_semaphore:
            host, port = parse_target(self.spec.target)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self.spec.connect_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise ConnectorError(
                    f"TCP connect to {host}:{port} timed out"
                ) from exc
            except OSError as exc:
                raise ConnectorError(f"TCP connect to {host}:{port} failed: {exc}") from exc

            try:
                banner = await self._read_banner(reader)
                challenge = parse_pow_challenge(banner)
                started = time.monotonic()
                x = await solve_pow_async(
                    challenge,
                    self._team_key,
                    timeout=self.spec.pow_timeout,
                )
                elapsed = time.monotonic() - started
                writer.write(x + b"\n")
                await writer.drain()
            except BaseException:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                raise

            async with self._lock:
                if self._stopping:
                    writer.close()
                    raise asyncio.CancelledError
                self._upstream_reader = reader
                self._upstream_writer = writer
                self._generation += 1
                self._pending.clear()
                self._last_error = None
                self._pow_summary = (
                    f"cleared with hmac-sha256 in {elapsed:.2f}s "
                    f"(nonce={challenge.nonce_hex}, bits={challenge.bits})"
                )
            self._store.system(
                f"upstream generation {self._generation} ready ({self._pow_summary})"
            )
            self._set_state(_READY)

    async def _read_banner(self, reader: asyncio.StreamReader) -> bytes:
        data = bytearray()
        deadline = asyncio.get_running_loop().time() + self.spec.banner_timeout
        while not _BANNER_RE.search(data):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ConnectorError("timed out waiting for PoW banner")
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise ConnectorError("timed out waiting for PoW banner") from exc
            if not chunk:
                raise ConnectorError("remote closed before PoW banner")
            data.extend(chunk)
            if len(data) > 131_072:
                raise ConnectorError("PoW banner exceeded 128 KiB")
        return bytes(data)

    async def _read_upstream(self) -> None:
        reader = self._upstream_reader
        if reader is None:
            return
        while not self._stopping:
            data = await reader.read(65_536)
            if not data:
                return
            self._store.record("C->A", data, source="upstream")
            await self._deliver_to_agent(data)

    async def _close_upstream(self) -> None:
        async with self._lock:
            self._upstream_reader = None
            writer = self._upstream_writer
            self._upstream_writer = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ------------------------------------------------------------------
    # Local agent lifecycle
    # ------------------------------------------------------------------
    async def _handle_agent(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._stopping:
            await _close_writer(writer)
            return
        pending = b""
        async with self._lock:
            upstream_ready = self._upstream_writer is not None and self._state == _READY
            busy = self._agent_writer is not None
            if not upstream_ready or busy:
                reason = b"busy: another agent is already attached\n" if busy else b"upstream not ready\n"
                with contextlib.suppress(Exception):
                    writer.write(b"[incypher-bridge] " + reason)
                    await writer.drain()
                await _close_writer(writer)
                self._store.system(
                    f"rejected agent connection ({'busy' if busy else 'not ready'})"
                )
                return
            self._agent_reader = reader
            self._agent_writer = writer
            pending = bytes(self._pending)
            self._pending.clear()
            if pending:
                try:
                    writer.write(pending)
                    await writer.drain()
                except Exception as exc:
                    self._last_error = f"pending replay failed: {type(exc).__name__}: {exc}"
                    self._agent_reader = None
                    self._agent_writer = None
                    writer.close()
                    return
        self._store.system("agent connected")
        try:
            if pending:
                self._store.system(f"replayed {len(pending)} buffered upstream bytes")
            while not self._stopping:
                data = await reader.read(65_536)
                if not data:
                    break
                self._store.record("A->C", data, source="agent")
                await self._send_upstream(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"agent relay error: {type(exc).__name__}: {exc}"
            self._store.system(self._last_error, level="WARN")
        finally:
            await self._detach_agent(writer)

    async def _deliver_to_agent(self, data: bytes) -> None:
        writer: asyncio.StreamWriter | None = None
        failure: str | None = None
        async with self._lock:
            writer = self._agent_writer
            if writer is None:
                self._append_pending_locked(data)
                return
            try:
                writer.write(data)
                await writer.drain()
                return
            except Exception as exc:
                failure = f"agent write failed: {type(exc).__name__}: {exc}"
                self._last_error = failure
                self._agent_reader = None
                self._agent_writer = None
                self._append_pending_locked(data)
        if failure:
            self._store.system(failure, level="WARN")
        if writer is not None:
            await _close_writer(writer)

    def _append_pending_locked(self, data: bytes) -> None:
        limit = self.spec.max_pending_bytes
        if len(data) >= limit:
            self._pending.clear()
            self._pending.extend(data[-limit:])
            self._store.system(
                f"pending buffer exceeded {limit} bytes; newest bytes retained",
                level="WARN",
            )
            return
        overflow = len(self._pending) + len(data) - limit
        if overflow > 0:
            del self._pending[:overflow]
            self._store.system(
                f"pending buffer overflow; dropped {overflow} oldest bytes",
                level="WARN",
            )
        self._pending.extend(data)

    async def _send_upstream(self, data: bytes) -> None:
        async with self._lock:
            writer = self._upstream_writer
        if writer is None:
            return
        try:
            writer.write(data)
            await writer.drain()
        except Exception as exc:
            self._last_error = f"upstream write failed: {type(exc).__name__}: {exc}"
            self._store.system(self._last_error, level="WARN")
            writer.close()

    async def _detach_agent(self, expected: asyncio.StreamWriter) -> None:
        async with self._lock:
            if self._agent_writer is not expected:
                return
            self._agent_writer = None
            self._agent_reader = None
        self._store.system("agent disconnected")
        self._updated_at = utc_now()
        self._write_meta()
        await _close_writer(expected)

    async def _close_agent(self) -> None:
        async with self._lock:
            writer = self._agent_writer
            self._agent_reader = None
            self._agent_writer = None
        if writer is not None:
            await _close_writer(writer)
            self._store.system("agent closed by bridge")


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


def parse_target(value: str) -> tuple[str, int]:
    text = str(value).strip()
    match = re.search(r"\bnc\b(?:\s+-\w+)*\s+(\S+)\s+(\d+)\b", text)
    if match:
        return match.group(1).strip("[]"), int(match.group(2))
    match = re.fullmatch(r"\[([^\]]+)\]\s*:\s*(\d+)", text)
    if match:
        return match.group(1), int(match.group(2))
    if text.count(":") == 1:
        host, port_text = text.rsplit(":", 1)
        if port_text.isdigit():
            return host.strip(), int(port_text)
    match = re.fullmatch(r"(\S+)\s+(\d+)", text)
    if match:
        return match.group(1).strip("[]"), int(match.group(2))
    raise ConnectorError(
        f"could not parse target {value!r}; use host:port, [ipv6]:port, or `nc HOST PORT`"
    )
