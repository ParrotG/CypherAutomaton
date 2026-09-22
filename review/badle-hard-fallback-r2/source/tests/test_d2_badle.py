from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
ARENA = ROOT / "deploy" / "arena"
sys.path.insert(0, str(ARENA))

from d2_badle import (  # noqa: E402
    BADLE_ARCHIVE_SHA256,
    BADLE_FLAG_SHA256,
    BADLE_HARD_FLAG_SHA256,
    BADLE_HARD_LOG_SHA256,
    BADLE_LOG_MEMBER_SHA256,
    D2_BADLE_FLAG_REGEX,
    BadLEError,
    BadLEExtraction,
    BadLEHardExtraction,
    analyze_badle_archive,
    analyze_badle_challenge,
    analyze_badle_hard_artifact,
    decode_badle_hard_log,
    decode_badle_log,
)


OXIMETER_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
CGM_UUID = "0000f003-0000-1000-8000-00805f9b34fb"


def _notify(uuid: str, payload: bytes, *, deprecated: bool = False) -> bytes:
    label = "BLE Notify (deprecated) <=" if deprecated else "BLE Notify <="
    encoded = "0x" + payload.hex(" ")
    return f"[{label}] UUID: {uuid} data: {encoded}".encode("ascii")


def _cgm_frame(encoded_reading: int, marker: int = 0) -> bytes:
    frame = bytearray.fromhex("af e8 86 80 00 6c 00 70 0f a2 b7 01 55 d2 00 00 00")
    frame[3] = marker & 0xFF
    frame[4] = encoded_reading
    return bytes(frame)


class BadLEDecoderTests(unittest.TestCase):
    def test_decodes_only_the_active_status_notification(self) -> None:
        active = bytes.fromhex("fe 0a 55 00 3e 62 23 51 48 ba")
        deprecated = bytes.fromhex("fe 0a 55 00 3e 32 23 51 48 ba")
        log = b"\n".join(
            (
                _notify(OXIMETER_UUID, bytes.fromhex("fe 08 56 38 00 08 08 a6")),
                _notify(OXIMETER_UUID, deprecated, deprecated=True),
                _notify(OXIMETER_UUID, active),
            )
        )

        result = decode_badle_log(log, D2_BADLE_FLAG_REGEX)

        self.assertEqual(result.spo2_percent, 98)
        self.assertEqual(result.flag, "flag{98}")
        self.assertEqual(result.frame_line_number, 3)

    def test_rejects_more_than_one_active_status_frame(self) -> None:
        first = bytes.fromhex("fe 0a 55 00 3e 62 23 51 48 ba")
        second = bytes.fromhex("fe 0a 55 00 3e 63 23 51 48 ba")
        log = b"\n".join(
            (_notify(OXIMETER_UUID, first), _notify(OXIMETER_UUID, second))
        )

        with self.assertRaisesRegex(BadLEError, "exactly one"):
            decode_badle_log(log, D2_BADLE_FLAG_REGEX)

    def test_rejects_an_impossible_spo2_value(self) -> None:
        frame = bytes.fromhex("fe 0a 55 00 3e ff 23 51 48 ba")

        with self.assertRaisesRegex(BadLEError, "SpO2"):
            decode_badle_log(_notify(OXIMETER_UUID, frame), D2_BADLE_FLAG_REGEX)


class BadLEHardDecoderTests(unittest.TestCase):
    def test_decodes_first_eight_active_reading_frames(self) -> None:
        readings_mg_dl = (90, 91, 92, 93, 94, 95, 96, 97, 120)
        lines = [_notify(CGM_UUID, bytes.fromhex("aa ea 80 67 c8"))]
        for index, reading in enumerate(readings_mg_dl):
            payload = _cgm_frame(reading ^ 0x7C, marker=index)
            lines.append(_notify(CGM_UUID, payload))
            lines.append(_notify(CGM_UUID, payload, deprecated=True))

        result = decode_badle_hard_log(b"\n".join(lines), D2_BADLE_FLAG_REGEX)

        self.assertEqual(result.glucose_mg_dl, readings_mg_dl[:8])
        self.assertEqual(
            result.glucose_mmol_l,
            ("5.0", "5.1", "5.1", "5.2", "5.2", "5.3", "5.3", "5.4"),
        )
        self.assertEqual(
            result.flag,
            "INCYPHER{5.0,5.1,5.1,5.2,5.2,5.3,5.3,5.4}",
        )
        self.assertEqual(result.frame_line_numbers, (2, 4, 6, 8, 10, 12, 14, 16))

    def test_rejects_fewer_than_eight_reading_frames(self) -> None:
        log = b"\n".join(
            _notify(CGM_UUID, _cgm_frame((90 + index) ^ 0x7C))
            for index in range(7)
        )

        with self.assertRaisesRegex(BadLEError, "at least eight"):
            decode_badle_hard_log(log, D2_BADLE_FLAG_REGEX)

    def test_rejects_a_malformed_relevant_notification(self) -> None:
        line = (
            b"[BLE Notify <=] UUID: "
            + CGM_UUID.encode("ascii")
            + b" data: 0xaf e8 not-hex"
        )

        with self.assertRaisesRegex(BadLEError, "malformed"):
            decode_badle_hard_log(line, D2_BADLE_FLAG_REGEX)

    def test_rejects_a_reading_outside_the_documented_range(self) -> None:
        lines = [
            _notify(CGM_UUID, _cgm_frame(reading ^ 0x7C))
            for reading in (71, 90, 91, 92, 93, 94, 95, 96)
        ]

        with self.assertRaisesRegex(BadLEError, "range"):
            decode_badle_hard_log(b"\n".join(lines), D2_BADLE_FLAG_REGEX)


class BadLEIdentityTests(unittest.TestCase):
    def test_rejects_a_mismatched_controller_identity_before_reading(self) -> None:
        missing = ROOT / "does-not-exist"

        with self.assertRaisesRegex(BadLEError, "identity"):
            analyze_badle_challenge(
                challenge_id=3,
                challenge_name="BadLE Hard",
                category="healthcare",
                challenge_type="standard",
                artifact_path=missing,
                flag_regex=D2_BADLE_FLAG_REGEX,
            )


BADLE_ARCHIVE = (
    ROOT / "arena-runs" / "d2-intake-20260922-1" / "3" / "APKlog.zip"
)
BADLE_HARD_LOG = (
    ROOT / "arena-runs" / "d2-intake-20260922-1" / "4" / "cgm_ble_log.txt"
)


@unittest.skipUnless(
    BADLE_ARCHIVE.is_file() and BADLE_HARD_LOG.is_file(),
    "exact D2 BadLE intake artifacts are not present",
)
class ExactD2BadLEReplayTests(unittest.TestCase):
    def test_badle_archive_is_digest_bound_and_reads_the_small_log_member(self) -> None:
        result = analyze_badle_archive(BADLE_ARCHIVE, D2_BADLE_FLAG_REGEX)

        self.assertEqual(result.archive_sha256, BADLE_ARCHIVE_SHA256)
        self.assertEqual(result.log_sha256, BADLE_LOG_MEMBER_SHA256)
        self.assertGreaterEqual(result.spo2_percent, 0)
        self.assertLessEqual(result.spo2_percent, 100)
        self.assertEqual(
            hashlib.sha256(result.flag.encode("ascii")).hexdigest(),
            BADLE_FLAG_SHA256,
        )

    def test_badle_hard_log_is_digest_bound(self) -> None:
        result = analyze_badle_hard_artifact(BADLE_HARD_LOG, D2_BADLE_FLAG_REGEX)

        self.assertEqual(result.log_sha256, BADLE_HARD_LOG_SHA256)
        self.assertEqual(len(result.glucose_mg_dl), 8)
        self.assertEqual(
            hashlib.sha256(result.flag.encode("ascii")).hexdigest(),
            BADLE_HARD_FLAG_SHA256,
        )

    def test_exact_identity_router_dispatches_both_challenges(self) -> None:
        badle = analyze_badle_challenge(
            challenge_id=3,
            challenge_name="BadLE",
            category="healthcare",
            challenge_type="standard",
            artifact_path=BADLE_ARCHIVE,
            flag_regex=D2_BADLE_FLAG_REGEX,
        )
        hard = analyze_badle_challenge(
            challenge_id=4,
            challenge_name="BadLE Hard",
            category="healthcare",
            challenge_type="standard",
            artifact_path=BADLE_HARD_LOG,
            flag_regex=D2_BADLE_FLAG_REGEX,
        )

        self.assertIsInstance(badle, BadLEExtraction)
        self.assertIsInstance(hard, BadLEHardExtraction)


if __name__ == "__main__":
    unittest.main()
