from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arena.runtime.model import ModelClient


class _Handler(BaseHTTPRequestHandler):
    received_headers: dict[str, str] = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).received_headers = {k.lower(): v for k, v in self.headers.items()}
        payload = {
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


class OpenRouterSmokeTests(unittest.TestCase):
    def test_openrouter_compatible_headers_are_sent(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ModelClient(
                api_key="test-key",
                base_url=f"http://127.0.0.1:{server.server_address[1]}/openrouter.ai/v1",
                model="deepseek-flash",
                max_retries=1,
            )
            reply = client.chat([{"role": "user", "content": "hi"}], tools=[])
            headers = dict(_Handler.received_headers)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(reply.message["content"], "OK")
        self.assertEqual(headers.get("authorization"), "Bearer test-key")
        self.assertEqual(headers.get("http-referer"), "https://in-cypher.com")
        self.assertEqual(headers.get("x-title"), "IN-CYPHER Arena")


if __name__ == "__main__":
    unittest.main()
