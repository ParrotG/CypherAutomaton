"""Bounded, digest-locked local analyzers for D2 BadLE artifacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import io
from pathlib import Path
import re
import zipfile


D2_BADLE_FLAG_REGEX = r"(?:INCYPHER|flag)\{[^{}\r\n]{1,256}\}"

BADLE_ARCHIVE_SHA256 = (
    "dc2cd4176c2d6ff38a7e2c8d0ae246ca0f92209a85351ec1b6526be8656982f2"
)
BADLE_LOG_MEMBER = "APK+log/oximeter_ble_log.txt"
BADLE_LOG_MEMBER_SHA256 = (
    "4cd9d9dac7dd5d1f6fc33c3e0fbae51d0ac2ba8c3ab15934b6ce7656bb57f18d"
)
BADLE_FLAG_SHA256 = (
    "395537e34a38a980fc880fa6584159eec7733e178276e8be50611f346a203811"
)

BADLE_HARD_LOG_SHA256 = (
    "4bc0a67815216988229cc1e2183bf7d5d4de4ab8008fdc2e38f7cfcb10680630"
)
BADLE_HARD_FLAG_SHA256 = (
    "0ead3e39de337c3285facb1039244faa5570b0588343e71fba7e309b49cf6ac5"
)

MAX_BADLE_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_BADLE_LOG_BYTES = 64 * 1024
MAX_BADLE_ZIP_MEMBERS = 16
MAX_LOG_LINES = 1024
MAX_LOG_LINE_BYTES = 2048
MAX_NOTIFICATION_BYTES = 256

_OXIMETER_UUID = b"0000ffe4-0000-1000-8000-00805f9b34fb"
_CGM_UUID = b"0000f003-0000-1000-8000-00805f9b34fb"
_STATUS_PREFIX = bytes.fromhex("fe 0a 55")
_CGM_READING_PREFIX = bytes.fromhex("af e8 86")
_CGM_CAPTURED_VALUE_INDEX = 4
_CGM_XOR_MASK = 0x7C
_NOTIFY_RE = re.compile(
    rb"^\[BLE Notify <=\] UUID: ([0-9a-fA-F-]{36}) data: (.+)$"
)
_HEX_BYTE_RE = re.compile(rb"^[0-9a-fA-F]{2}$")


class BadLEError(ValueError):
    """Raised when a locked BadLE artifact or capture is invalid."""


@dataclass(frozen=True)
class BadLEExtraction:
    flag: str
    spo2_percent: int
    frame_line_number: int
    log_sha256: str
    archive_sha256: str | None = None


@dataclass(frozen=True)
class BadLEHardExtraction:
    flag: str
    glucose_mg_dl: tuple[int, ...]
    glucose_mmol_l: tuple[str, ...]
    frame_line_numbers: tuple[int, ...]
    log_sha256: str


def _compile_flag_pattern(flag_regex: str) -> re.Pattern[bytes]:
    try:
        return re.compile(flag_regex.encode("ascii"))
    except (UnicodeEncodeError, re.error) as exc:
        raise BadLEError("sealed flag pattern is not valid ASCII regex") from exc


def _candidate(body: str, flag_regex: str, *, prefix: str = "flag") -> str:
    if prefix not in {"flag", "INCYPHER"}:
        raise BadLEError("derived candidate prefix is not supported")
    try:
        encoded = f"{prefix}{{{body}}}".encode("ascii")
    except UnicodeEncodeError as exc:
        raise BadLEError("derived candidate is not ASCII") from exc
    if _compile_flag_pattern(flag_regex).fullmatch(encoded) is None:
        raise BadLEError("derived candidate does not match the sealed flag pattern")
    return encoded.decode("ascii")


def _parse_payload(raw: bytes) -> bytes:
    tokens = raw.split()
    if not tokens or not tokens[0].startswith(b"0x"):
        raise BadLEError("relevant BLE notification has malformed hex payload")
    first = tokens[0][2:]
    if _HEX_BYTE_RE.fullmatch(first) is None:
        raise BadLEError("relevant BLE notification has malformed hex payload")
    values = [int(first, 16)]
    for token in tokens[1:]:
        if _HEX_BYTE_RE.fullmatch(token) is None:
            raise BadLEError("relevant BLE notification has malformed hex payload")
        values.append(int(token, 16))
    if len(values) > MAX_NOTIFICATION_BYTES:
        raise BadLEError("BLE notification exceeds the frame byte limit")
    return bytes(values)


def _notifications(log_data: bytes, target_uuid: bytes) -> list[tuple[int, bytes]]:
    if len(log_data) > MAX_BADLE_LOG_BYTES:
        raise BadLEError("BLE capture exceeds the analyzer byte limit")
    lines = log_data.splitlines()
    if len(lines) > MAX_LOG_LINES:
        raise BadLEError("BLE capture exceeds the analyzer line limit")

    notifications: list[tuple[int, bytes]] = []
    for line_number, line in enumerate(lines, start=1):
        if len(line) > MAX_LOG_LINE_BYTES:
            raise BadLEError("BLE capture line exceeds the analyzer byte limit")
        lower = line.lower()
        if not lower.startswith(b"[ble notify <=]"):
            continue
        match = _NOTIFY_RE.fullmatch(line)
        if match is None:
            if target_uuid in lower:
                raise BadLEError("relevant BLE notification is malformed")
            continue
        if match.group(1).lower() != target_uuid:
            continue
        notifications.append((line_number, _parse_payload(match.group(2))))
    return notifications


def decode_badle_log(
    log_data: bytes,
    flag_regex: str = D2_BADLE_FLAG_REGEX,
) -> BadLEExtraction:
    """Decode ID 3 from a bounded oximeter capture without touching the APK."""

    frames = [
        (line_number, payload)
        for line_number, payload in _notifications(log_data, _OXIMETER_UUID)
        if len(payload) == 10 and payload.startswith(_STATUS_PREFIX)
    ]
    if len(frames) != 1:
        raise BadLEError("BLE capture must contain exactly one active status frame")
    line_number, frame = frames[0]
    spo2_percent = frame[5]
    if not 0 <= spo2_percent <= 100:
        raise BadLEError("decoded SpO2 percentage is outside the valid range")
    return BadLEExtraction(
        flag=_candidate(str(spo2_percent), flag_regex),
        spo2_percent=spo2_percent,
        frame_line_number=line_number,
        log_sha256=hashlib.sha256(log_data).hexdigest(),
    )


def decode_badle_hard_log(
    log_data: bytes,
    flag_regex: str = D2_BADLE_FLAG_REGEX,
) -> BadLEHardExtraction:
    """Decode ID 4's first eight active CGM reading notifications."""

    readings = [
        (line_number, payload)
        for line_number, payload in _notifications(log_data, _CGM_UUID)
        if len(payload) == 17 and payload.startswith(_CGM_READING_PREFIX)
    ]
    if len(readings) < 8:
        raise BadLEError("BLE capture must contain at least eight active reading frames")

    selected = readings[:8]
    # The challenge names frame[6], while the displayed payload's index 6 is
    # nonphysiological. Index 4 is independently selected by the supplied
    # 4--12 mmol/L smoothness oracle. No two-byte prefix is visible in the log;
    # the numbering discrepancy's origin and platform acceptance are unverified.
    glucose_mg_dl = tuple(
        payload[_CGM_CAPTURED_VALUE_INDEX] ^ _CGM_XOR_MASK
        for _, payload in selected
    )
    if any(not 72 <= reading <= 216 for reading in glucose_mg_dl):
        raise BadLEError("decoded glucose reading is outside the documented range")
    glucose_mmol_l = tuple(
        format(
            (Decimal(reading) / Decimal(18)).quantize(
                Decimal("0.1"),
                rounding=ROUND_HALF_UP,
            ),
            ".1f",
        )
        for reading in glucose_mg_dl
    )
    return BadLEHardExtraction(
        flag=_candidate(",".join(glucose_mmol_l), flag_regex, prefix="INCYPHER"),
        glucose_mg_dl=glucose_mg_dl,
        glucose_mmol_l=glucose_mmol_l,
        frame_line_numbers=tuple(line_number for line_number, _ in selected),
        log_sha256=hashlib.sha256(log_data).hexdigest(),
    )


def _validate_local_path(path: str | Path, max_bytes: int) -> Path:
    artifact = Path(path)
    if artifact.is_symlink():
        raise BadLEError("artifact must not be a symbolic link")
    if not artifact.is_file():
        raise BadLEError("artifact is not a regular file")
    try:
        size = artifact.stat().st_size
    except OSError as exc:
        raise BadLEError("artifact metadata could not be read") from exc
    if size > max_bytes:
        raise BadLEError("artifact exceeds the analyzer byte limit")
    return artifact


def _read_bounded(path: str | Path, max_bytes: int) -> bytes:
    artifact = _validate_local_path(path, max_bytes)
    try:
        with artifact.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise BadLEError("artifact could not be read") from exc
    if len(data) > max_bytes:
        raise BadLEError("artifact exceeds the analyzer byte limit")
    return data


def analyze_badle_archive(
    archive_path: str | Path,
    flag_regex: str = D2_BADLE_FLAG_REGEX,
) -> BadLEExtraction:
    """Validate the exact ID 3 archive and read only its small capture member."""

    archive_data = _read_bounded(archive_path, MAX_BADLE_ARCHIVE_BYTES)
    return analyze_badle_archive_bytes(archive_data, flag_regex)


def analyze_badle_archive_bytes(
    archive_data: bytes,
    flag_regex: str = D2_BADLE_FLAG_REGEX,
) -> BadLEExtraction:
    """Validate exact archive bytes and read only their small capture member."""

    if (
        not isinstance(archive_data, bytes)
        or not archive_data
        or len(archive_data) > MAX_BADLE_ARCHIVE_BYTES
    ):
        raise BadLEError("BadLE archive is empty or exceeds the analyzer byte limit")
    archive_sha256 = hashlib.sha256(archive_data).hexdigest()
    if not hmac.compare_digest(archive_sha256, BADLE_ARCHIVE_SHA256):
        raise BadLEError("BadLE archive digest does not match the locked input")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_BADLE_ZIP_MEMBERS:
                raise BadLEError("BadLE archive exceeds the member-count limit")
            matches = [info for info in infos if info.filename == BADLE_LOG_MEMBER]
            if len(matches) != 1:
                raise BadLEError("BadLE archive must contain one exact log member")
            info = matches[0]
            if info.is_dir() or info.flag_bits & 0x1:
                raise BadLEError("BadLE log member is not a readable regular entry")
            if (
                info.file_size > MAX_BADLE_LOG_BYTES
                or info.compress_size > MAX_BADLE_LOG_BYTES
            ):
                raise BadLEError("BadLE log member exceeds the analyzer byte limit")
            with archive.open(info, "r") as member:
                log_data = member.read(MAX_BADLE_LOG_BYTES + 1)
            if len(log_data) != info.file_size:
                raise BadLEError("BadLE log member size is inconsistent")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BadLEError("BadLE archive could not be read safely") from exc

    log_sha256 = hashlib.sha256(log_data).hexdigest()
    if not hmac.compare_digest(log_sha256, BADLE_LOG_MEMBER_SHA256):
        raise BadLEError("BadLE log member digest does not match the locked input")
    return replace(
        decode_badle_log(log_data, flag_regex),
        archive_sha256=archive_sha256,
    )


def analyze_badle_hard_artifact(
    log_path: str | Path,
    flag_regex: str = D2_BADLE_FLAG_REGEX,
) -> BadLEHardExtraction:
    """Validate and decode the exact ID 4 standalone capture."""

    log_data = _read_bounded(log_path, MAX_BADLE_LOG_BYTES)
    log_sha256 = hashlib.sha256(log_data).hexdigest()
    if not hmac.compare_digest(log_sha256, BADLE_HARD_LOG_SHA256):
        raise BadLEError("BadLE Hard log digest does not match the locked input")
    return decode_badle_hard_log(log_data, flag_regex)


def analyze_badle_challenge(
    *,
    challenge_id: int,
    challenge_name: str,
    category: str,
    challenge_type: str,
    artifact_path: str | Path,
    flag_regex: str = D2_BADLE_FLAG_REGEX,
) -> BadLEExtraction | BadLEHardExtraction:
    """Dispatch only exact controller-owned D2 BadLE identities."""

    identity = (challenge_id, challenge_name, category, challenge_type)
    artifact = Path(artifact_path)
    if identity == (3, "BadLE", "healthcare", "standard"):
        if artifact.name != "APKlog.zip":
            raise BadLEError("BadLE artifact filename does not match the locked identity")
        return analyze_badle_archive(artifact, flag_regex)
    if identity == (4, "BadLE Hard", "healthcare", "standard"):
        if artifact.name != "cgm_ble_log.txt":
            raise BadLEError("BadLE Hard artifact filename does not match the locked identity")
        return analyze_badle_hard_artifact(artifact, flag_regex)
    raise BadLEError("challenge identity is not an exact supported BadLE target")


__all__ = [
    "BADLE_ARCHIVE_SHA256",
    "BADLE_FLAG_SHA256",
    "BADLE_HARD_FLAG_SHA256",
    "BADLE_HARD_LOG_SHA256",
    "BADLE_LOG_MEMBER",
    "BADLE_LOG_MEMBER_SHA256",
    "D2_BADLE_FLAG_REGEX",
    "BadLEError",
    "BadLEExtraction",
    "BadLEHardExtraction",
    "analyze_badle_archive",
    "analyze_badle_archive_bytes",
    "analyze_badle_challenge",
    "analyze_badle_hard_artifact",
    "decode_badle_hard_log",
    "decode_badle_log",
]
