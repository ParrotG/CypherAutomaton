from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from incypher_agent.model import ModelClient


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = {
            "id": "chatcmpl-test",
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


class _FakeMessage:
    def model_dump(self, exclude_none: bool = False) -> dict:
        return {"role": "assistant", "content": "OK"}


class _FakeUsage:
    prompt_tokens = 1
    completion_tokens = 1
    total_tokens = 2

    def model_dump(self) -> dict:
        return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class AgentModelTests(unittest.TestCase):
    def test_transient_connection_errors_are_retried(self) -> None:
        client = ModelClient(
            api_key="test",
            base_url="http://127.0.0.1:1/v1",
            model="deepseek-flash",
            max_retries=3,
            retry_base=0.001,
            retry_max_wait=0.002,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=_FakeMessage())],
            usage=_FakeUsage(),
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[ConnectionError("one"), ConnectionError("two"), response],
        ):
            reply = client.chat([{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(reply.message["content"], "OK")
        self.assertEqual(reply.total_tokens, 2)

    def test_openai_compatible_call(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ModelClient(
                api_key="test",
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                model="deepseek-flash",
                max_retries=1,
            )
            reply = client.chat(
                [{"role": "user", "content": "hi"}],
                tools=[],
            )
            self.assertEqual(reply.message["content"], "OK")
            self.assertEqual(reply.total_tokens, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
