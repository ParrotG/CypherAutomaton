"""Proof-of-work solver for the IN-CYPHER raw-TCP gate.

The live gate uses this exact relation:

    sha256(hmac_sha256(team_key, nonce) || X)

where ``nonce`` is the ASCII hex string from the banner and ``X`` is sent as
the ASCII decimal counter followed by a newline.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from dataclasses import dataclass

_NONCE_RE = re.compile(rb"nonce=([0-9a-fA-F]+)\s+bits=(\d+)")
_DENIAL_RE = re.compile(
    rb"denied:\s*invalid\s+proof"
    rb"|invalid\s+proof\s*\("
    rb"|proof\s+rejected"
    rb"|wrong\s+team\s*key"
    rb"|insufficient\s+work",
    re.IGNORECASE,
)


class PowError(RuntimeError):
    """Raised when a PoW challenge cannot be parsed or solved."""


class PowRejected(PowError):
    """Raised when the remote gate explicitly rejects the proof."""


@dataclass(frozen=True)
class PowChallenge:
    nonce_hex: str
    bits: int


def parse_pow_challenge(text: bytes) -> PowChallenge:
    match = _NONCE_RE.search(text)
    if not match:
        raise PowError("Could not find `nonce=... bits=...` in server banner")
    nonce = match.group(1).decode("ascii")
    bits = int(match.group(2))
    if bits <= 0 or bits > 256:
        raise PowError(f"Invalid PoW difficulty: {bits}")
    return PowChallenge(nonce_hex=nonce, bits=bits)


def leading_zero_bits_ok(digest: bytes, bits: int) -> bool:
    full_bytes, remaining = divmod(bits, 8)
    if any(byte != 0 for byte in digest[:full_bytes]):
        return False
    if remaining and (digest[full_bytes] >> (8 - remaining)):
        return False
    return True


def solve_pow(
    challenge: PowChallenge,
    team_key: str | bytes,
    *,
    timeout: float | None = 120.0,
) -> bytes:
    """Return the ASCII decimal X that satisfies ``challenge``."""

    key = team_key.encode("utf-8") if isinstance(team_key, str) else bytes(team_key)
    mac = hmac.new(key, challenge.nonce_hex.encode("ascii"), hashlib.sha256).digest()
    started = time.monotonic()
    counter = 0
    sha256 = hashlib.sha256
    while True:
        candidate = str(counter).encode("ascii")
        if leading_zero_bits_ok(
            sha256(mac + candidate).digest(), challenge.bits
        ):
            return candidate
        counter += 1
        if timeout is not None and (counter & 0xFFFF) == 0:
            if time.monotonic() - started >= timeout:
                raise PowError(
                    f"PoW timed out after {timeout:.1f}s "
                    f"({counter:,} tries, difficulty {challenge.bits})"
                )
        if counter >= 2**63:
            raise PowError("PoW counter exhausted")


async def solve_pow_async(
    challenge: PowChallenge,
    team_key: str | bytes,
    *,
    timeout: float | None = 120.0,
) -> bytes:
    """Run :func:`solve_pow` in a worker thread."""

    return await asyncio.to_thread(
        solve_pow,
        challenge,
        team_key,
        timeout=timeout,
    )


def looks_denied(data: bytes) -> bool:
    return bool(_DENIAL_RE.search(data))
