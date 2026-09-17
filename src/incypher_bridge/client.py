"""Raw TCP helper that clears the IN-CYPHER team-key-bound PoW gate.

The public entry point deliberately mirrors the promised official helper::

    from solver import connect
    s = connect("47.236.162.54", 30214, team_key)

The official gate implementation was not published when this project was
written, so ``connect`` tries a short list of plausible encodings.  The first
variant is the most likely simple Python implementation::

    mac = hmac.new(team_key.encode(), nonce.encode(), sha256).digest()
    sha256(mac + str(i).encode()).digest()
"""

from __future__ import annotations

import errno
import re
import socket
import time
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from .pow import (
    DEFAULT_VARIANTS,
    PowError,
    PowRejected,
    PowVariant,
    XEncoding,
    looks_denied,
    parse_pow_challenge,
    solve_pow,
)

EventSink = Callable[[str, Any], None]
_BANNER_RE = re.compile(rb"nonce=[0-9a-fA-F]+\s+bits=\d+")


def _emit(sink: EventSink | None, kind: str, payload: Any) -> None:
    if sink is None:
        return
    try:
        sink(kind, payload)
    except Exception:
        # Logging must never break the protocol path.
        pass


class PrefixedSocket:
    """Socket-like wrapper that replays bytes consumed during the PoW handshake.

    The gate handshake must read slightly beyond the prompt to decide whether
    the proof was accepted.  Those bytes can already belong to the challenge
    service, so they are replayed to the caller before reading the raw socket.
    """

    def __init__(self, sock: socket.socket, prefix: bytes = b"") -> None:
        self._sock = sock
        self._prefix = bytearray(prefix)
        # Metadata filled in by client._try_variant, useful for the bridge UI.
        self.variant_name: str | None = None
        self.banner: bytes = b""
        self.challenge: Any = None
        self.pow_seconds: float | None = None

    # -- socket-like API -------------------------------------------------
    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        if self._prefix:
            data = bytes(self._prefix[:bufsize])
            del self._prefix[:bufsize]
            return data
        return self._sock.recv(bufsize, flags)

    def recv_into(self, buffer, nbytes: int = 0, flags: int = 0) -> int:
        if self._prefix:
            view = memoryview(buffer).cast("B")
            take = min(nbytes or len(view), len(self._prefix), len(view))
            view[:take] = self._prefix[:take]
            del self._prefix[:take]
            return take
        return self._sock.recv_into(buffer, nbytes, flags)

    def sendall(self, data: bytes, flags: int = 0) -> None:
        self._sock.sendall(data, flags)

    def send(self, data: bytes, flags: int = 0) -> int:
        return self._sock.send(data, flags)

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def gettimeout(self) -> float | None:
        return self._sock.gettimeout()

    def setblocking(self, flag: bool) -> None:
        self._sock.setblocking(flag)

    def fileno(self) -> int:
        return self._sock.fileno()

    def shutdown(self, how: int) -> None:
        self._sock.shutdown(how)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "PrefixedSocket":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __getattr__(self, name: str):
        return getattr(self._sock, name)

    @property
    def closed(self) -> bool:
        return self._sock.fileno() < 0

    @property
    def raw_socket(self) -> socket.socket:
        return self._sock


def _recv_until(
    sock: socket.socket,
    predicate: Callable[[bytes], bool],
    *,
    timeout: float,
    max_bytes: int = 131072,
) -> bytes:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline and len(data) < max_bytes:
        remaining = max(0.05, deadline - time.monotonic())
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        except OSError as exc:
            if exc.errno in (errno.ECONNRESET, errno.EBADF):
                break
            raise
        if not chunk:
            break
        data.extend(chunk)
        if predicate(bytes(data)):
            break
    return bytes(data)


def _collect_after_proof(
    sock: socket.socket,
    *,
    idle_timeout: float = 0.25,
    max_wait: float = 1.0,
) -> bytes:
    """Read the immediate server response after sending X.

    The service may or may not send an explicit success line.  We read until
    the stream is idle, a denial appears, or ``max_wait`` elapses.  Whatever is
    read is retained and replayed to the caller by :class:`PrefixedSocket`.
    """

    data = bytearray()
    deadline = time.monotonic() + max_wait
    idle_deadline = time.monotonic() + idle_timeout

    while time.monotonic() < deadline:
        wait = min(deadline - time.monotonic(), idle_deadline - time.monotonic())
        if wait <= 0:
            if data:
                break
            wait = 0.05

        sock.settimeout(max(0.01, wait))
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            if data:
                break
            continue
        except OSError:
            break

        if not chunk:
            break

        data.extend(chunk)
        if looks_denied(bytes(data)):
            break
        idle_deadline = time.monotonic() + idle_timeout

    return bytes(data)


def _is_socket_closed(sock: socket.socket, timeout: float = 0.05) -> bool:
    """Return True if the peer has closed the connection.

    A short ``MSG_PEEK`` avoids mistaking a live but quiet service for a closed
    one.
    """

    old_timeout = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        try:
            return sock.recv(1, socket.MSG_PEEK) == b""
        except socket.timeout:
            return False
        except BlockingIOError:
            return False
        except OSError:
            return True
    finally:
        sock.settimeout(old_timeout)


def _try_variant(
    host: str,
    port: int,
    team_key: str | bytes,
    variant: PowVariant,
    *,
    connect_timeout: float,
    read_timeout: float,
    pow_timeout: float | None,
    after_proof_idle: float,
    after_proof_max: float,
    progress_every: int | None,
    verbose: bool,
    on_event: EventSink | None,
    cancel_event: threading.Event | None = None,
    on_socket: Callable[[socket.socket], None] | None = None,
) -> PrefixedSocket:
    _emit(on_event, "pow_attempt", {"variant": variant.name})
    if verbose:
        print(f"[*] PoW variant: {variant.name}", flush=True)

    sock = socket.create_connection((host, port), timeout=connect_timeout)
    try:
        if on_socket is not None:
            on_socket(sock)
        banner = _recv_until(sock, lambda b: bool(_BANNER_RE.search(b)), timeout=read_timeout)
        if not banner:
            raise PowError("Connection closed before the PoW banner")
        _emit(on_event, "pow_banner", banner)
        if looks_denied(banner):
            raise PowRejected("Server denied the connection before PoW")

        challenge = parse_pow_challenge(banner)
        if verbose:
            print(
                f"[*] challenge nonce={challenge.nonce_hex} bits={challenge.bits}; solving...",
                flush=True,
            )
        started = time.monotonic()
        x = solve_pow(
            challenge,
            team_key,
            variant=variant,
            timeout=pow_timeout,
            progress_every=progress_every,
            cancel_event=cancel_event,
        )
        elapsed = time.monotonic() - started
        if verbose:
            print(f"[*] solved in {elapsed:.2f}s, sending X={x!r}", flush=True)
        _emit(
            on_event,
            "pow_solved",
            {
                "variant": variant.name,
                "nonce": challenge.nonce_hex,
                "bits": challenge.bits,
                "seconds": elapsed,
            },
        )

        sock.sendall(x + b"\n")
        response = _collect_after_proof(
            sock,
            idle_timeout=after_proof_idle,
            max_wait=after_proof_max,
        )
        if looks_denied(response):
            _emit(on_event, "pow_denied", response)
            raise PowRejected("Server rejected proof (wrong key/variant or insufficient work)")
        if not response and _is_socket_closed(sock):
            _emit(on_event, "pow_closed", b"")
            raise PowRejected("Server closed the connection after the proof without accepting it")

        wrapped = PrefixedSocket(sock, response)
        wrapped.variant_name = variant.name
        wrapped.banner = banner
        wrapped.challenge = challenge
        wrapped.pow_seconds = elapsed
        _emit(on_event, "pow_accepted", {"variant": variant.name, "initial_bytes": len(response)})
        return wrapped
    except BaseException:
        try:
            sock.close()
        finally:
            raise


def connect(
    host: str,
    port: int,
    team_key: str | bytes | None = None,
    *,
    variants: Sequence[PowVariant] | None = None,
    connect_timeout: float = 10.0,
    read_timeout: float = 10.0,
    pow_timeout: float | None = 120.0,
    after_proof_idle: float = 0.25,
    after_proof_max: float = 1.0,
    retry_delay: float = 0.2,
    progress_every: int | None = None,
    verbose: bool = False,
    on_event: EventSink | None = None,
    cancel_event: threading.Event | None = None,
    on_socket: Callable[[socket.socket], None] | None = None,
) -> PrefixedSocket:
    """Connect to an IN-CYPHER raw-TCP instance and clear its PoW gate.

    ``team_key`` may be ``None``, in which case it is loaded securely from the
    environment / ``.env.local``.  The key is used locally for HMAC and is
    never sent over the socket.
    """

    if team_key is None:
        from .config import load_team_key

        env_file: str | Path | None = Path.cwd() / ".env.local"
        if not Path(env_file).exists():
            project_env = Path(__file__).resolve().parents[2] / ".env.local"
            if project_env.exists():
                env_file = project_env
        team_key = load_team_key(env_file=env_file)

    candidate_list = tuple(variants or DEFAULT_VARIANTS)
    failures: list[str] = []
    for index, variant in enumerate(candidate_list):
        if cancel_event is not None and cancel_event.is_set():
            raise PowError("Connection cancelled")
        try:
            return _try_variant(
                host,
                port,
                team_key,
                variant,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                pow_timeout=pow_timeout,
                after_proof_idle=after_proof_idle,
                after_proof_max=after_proof_max,
                progress_every=progress_every,
                verbose=verbose,
                on_event=on_event,
                cancel_event=cancel_event,
                on_socket=on_socket,
            )
        except PowRejected as exc:
            failures.append(f"{variant.name}: {exc}")
            if index + 1 < len(candidate_list):
                if cancel_event is not None:
                    cancel_event.wait(max(0.0, retry_delay))
                else:
                    time.sleep(max(0.0, retry_delay))
            continue

    raise PowError(
        "All PoW variants were rejected. The gate may use a different encoding; "
        "try adding a custom PowVariant. Details: " + " | ".join(failures)
    )


def connect_local(
    port: int,
    host: str = "127.0.0.1",
    *,
    timeout: float = 10.0,
) -> socket.socket:
    """Connect an agent to the local bridge endpoint (no PoW, no team key)."""

    return socket.create_connection((host, port), timeout=timeout)
