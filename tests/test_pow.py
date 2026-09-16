from __future__ import annotations

import hashlib
import hmac
import unittest

from incypher_bridge.pow import PowChallenge, parse_pow_challenge, solve_pow


class PowTests(unittest.TestCase):
    def test_parse_banner(self) -> None:
        banner = (
            b"IN-CYPHER pwn instance proof-of-work required\n"
            b"nonce=3b813612dc09ad98085b2a8f55305547 bits=20\n"
            b"send X such that ..."
        )
        challenge = parse_pow_challenge(banner)
        self.assertEqual(challenge.nonce_hex, "3b813612dc09ad98085b2a8f55305547")
        self.assertEqual(challenge.bits, 20)

    def test_solve_first_variant(self) -> None:
        team_key = b"test-team-key"
        nonce = "00112233445566778899aabbccddeeff"
        bits = 10
        challenge = PowChallenge(nonce_hex=nonce, bits=bits)
        x = solve_pow(challenge, team_key, timeout=30.0)
        mac = hmac.new(team_key, nonce.encode("ascii"), hashlib.sha256).digest()
        digest = hashlib.sha256(mac + x).digest()
        full, remaining = divmod(bits, 8)
        self.assertTrue(all(byte == 0 for byte in digest[:full]))
        if remaining:
            self.assertEqual(digest[full] >> (8 - remaining), 0)


if __name__ == "__main__":
    unittest.main()
