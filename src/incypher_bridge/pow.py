"""Proof-of-work parsing and solving for the IN-CYPHER raw-TCP gate."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Iterator

# The bootstrap banner is emitted before the PoW prompt.  Keep the parser
# tolerant because TCP reads split anywhere.
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
    """Base class for PoW failures."""


class PowRejected(PowError):
    """The server rejected the proof (wrong variant, key, or too little work)."""


@dataclass(frozen=True)
class PowChallenge:
    nonce_hex: str
    bits: int


class NonceEncoding(str, Enum):
    ASCII_HEX = "ascii-hex"
    RAW = "raw"


class XEncoding(str, Enum):
    DECIMAL = "decimal"
    HEX = "hex"
    RAW_BE = "raw-be"
    RAW_LE = "raw-le"


@dataclass(frozen=True)
class PowVariant:
    """A guess at how the server computes/compares the puzzle.

    These variants exist because the official `solver` helper was not
    published.  The first variant is the most likely simple Python
    implementation:

        hmac.new(team_key.encode(), nonce.encode(), sha256).digest()
        sha256(digest + str(i).encode()).digest()
    """

    name: str
    nonce_encoding: NonceEncoding = NonceEncoding.ASCII_HEX
    x_encoding: XEncoding = XEncoding.DECIMAL
    mac_encoding: str = "raw"  # "raw" HMAC digest bytes or "hex" ASCII hexdigest
    key_encoding: str = "utf-8"


DEFAULT_VARIANTS: tuple[PowVariant, ...] = (
    # Most likely simple Python implementation: raw HMAC digest, ASCII nonce,
    # decimal X sent as text.
    PowVariant(
        "rawmac-ascii-nonce/decimal-x",
        NonceEncoding.ASCII_HEX,
        XEncoding.DECIMAL,
        mac_encoding="raw",
    ),
    PowVariant(
        "rawmac-raw-nonce/decimal-x",
        NonceEncoding.RAW,
        XEncoding.DECIMAL,
        mac_encoding="raw",
    ),
    PowVariant(
        "rawmac-ascii-nonce/hex-x",
        NonceEncoding.ASCII_HEX,
        XEncoding.HEX,
        mac_encoding="raw",
    ),
    PowVariant(
        "rawmac-raw-nonce/hex-x",
        NonceEncoding.RAW,
        XEncoding.HEX,
        mac_encoding="raw",
    ),
    # Some implementations name the helper hmac_sha256(...) and return a
    # hexdigest string; try that interpretation too.
    PowVariant(
        "hexmac-ascii-nonce/decimal-x",
        NonceEncoding.ASCII_HEX,
        XEncoding.DECIMAL,
        mac_encoding="hex",
    ),
    PowVariant(
        "hexmac-raw-nonce/decimal-x",
        NonceEncoding.RAW,
        XEncoding.DECIMAL,
        mac_encoding="hex",
    ),
    PowVariant(
        "hexmac-ascii-nonce/hex-x",
        NonceEncoding.ASCII_HEX,
        XEncoding.HEX,
        mac_encoding="hex",
    ),
    PowVariant(
        "hexmac-raw-nonce/hex-x",
        NonceEncoding.RAW,
        XEncoding.HEX,
        mac_encoding="hex",
    ),
    # Less likely raw integer bytes; kept as late fallbacks.
    PowVariant(
        "rawmac-ascii-nonce/raw-be-x",
        NonceEncoding.ASCII_HEX,
        XEncoding.RAW_BE,
        mac_encoding="raw",
    ),
    PowVariant(
        "rawmac-raw-nonce/raw-be-x",
        NonceEncoding.RAW,
        XEncoding.RAW_BE,
        mac_encoding="raw",
    ),
)


def parse_pow_challenge(text: bytes) -> PowChallenge:
    match = _NONCE_RE.search(text)
    if not match:
        raise PowError("Could not find `nonce=... bits=...` in server banner")
    nonce = match.group(1).decode("ascii")
    bits = int(match.group(2))
    if bits <= 0 or bits > 256:
        raise PowError(f"Invalid PoW difficulty: {bits}")
    return PowChallenge(nonce, bits)


def looks_denied(data: bytes) -> bool:
    return bool(_DENIAL_RE.search(data))


def _nonce_bytes(nonce_hex: str, encoding: NonceEncoding) -> bytes:
    if encoding is NonceEncoding.RAW:
        return bytes.fromhex(nonce_hex)
    return nonce_hex.encode("ascii")


def _x_bytes(counter: int, encoding: XEncoding) -> bytes:
    if encoding is XEncoding.DECIMAL:
        return str(counter).encode("ascii")
    if encoding is XEncoding.HEX:
        return format(counter, "x").encode("ascii")
    if encoding is XEncoding.RAW_BE:
        return counter.to_bytes(8, "big")
    if encoding is XEncoding.RAW_LE:
        return counter.to_bytes(8, "little")
    raise AssertionError(f"unknown X encoding: {encoding}")


def _leading_zero_bits_ok(digest: bytes, bits: int) -> bool:
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
    variant: PowVariant = DEFAULT_VARIANTS[0],
    timeout: float | None = 120.0,
    progress_every: int | None = None,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Return the X bytes that satisfy *challenge* for *variant*.

    Raises :class:`PowError` if no proof is found before *timeout*.
    """

    if isinstance(team_key, str):
        key = team_key.encode(variant.key_encoding)
    else:
        key = bytes(team_key)

    message = _nonce_bytes(challenge.nonce_hex, variant.nonce_encoding)
    mac = hmac.new(key, message, hashlib.sha256).digest()
    if variant.mac_encoding == "hex":
        prefix = mac.hex().encode("ascii")
    elif variant.mac_encoding == "raw":
        prefix = mac
    else:
        raise PowError(f"Unknown HMAC encoding: {variant.mac_encoding!r}")

    started = time.monotonic()
    counter = 0
    sha256 = hashlib.sha256
    while True:
        if (counter & 0xFFFF) == 0 and cancel_event is not None and cancel_event.is_set():
            raise PowError("PoW cancelled")
        candidate = _x_bytes(counter, variant.x_encoding)
        if _leading_zero_bits_ok(sha256(prefix + candidate).digest(), challenge.bits):
            return candidate

        counter += 1
        if progress_every and counter % progress_every == 0:
            elapsed = time.monotonic() - started
            # Kept on stderr by the CLI, never mixed into protocol data.
            print(
                f"  ... PoW {variant.name}: {counter:,} tries, {elapsed:.1f}s",
                flush=True,
            )

        if timeout is not None and (counter & 0xFFFF) == 0:
            if time.monotonic() - started > timeout:
                raise PowError(
                    f"PoW timed out after {timeout:.1f}s "
                    f"({counter:,} tries, difficulty {challenge.bits})"
                )

        # Raw encodings can only represent 64-bit counters.
        if variant.x_encoding in (XEncoding.RAW_BE, XEncoding.RAW_LE) and counter >= 2**64:
            raise PowError(f"Exhausted 64-bit counter for {variant.name}")


def all_variants() -> Iterator[PowVariant]:
    yield from DEFAULT_VARIANTS
