"""Real loopback HTTP deadlines; no external model service or paid calls."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from future_agent.application.ports import ModelError
from future_agent.config import Settings
from future_agent.providers.compatible import CompatibleProvider


@pytest.mark.parametrize("phase", ["headers", "body", "normal"])
def test_deadline_cancels_slow_headers_and_continuous_body(phase, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    stopping = threading.Event()
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            body = b'{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]}'
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                self.wfile.flush()
                if phase == "headers":
                    for i in range(60):
                        self.wfile.write(f"X-Delay-{i}: yes\r\n".encode())
                        self.wfile.flush()
                        if stopping.wait(0.025):
                            return
                self.wfile.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
                self.wfile.flush()
                if phase == "body":
                    for value in body:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        if stopping.wait(0.025):
                            return
                else:
                    self.wfile.write(body)
                    self.wfile.flush()
            except OSError:
                # Deadline cancellation closes the client connection during the response.
                return

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    provider = CompatibleProvider(
        Settings(llm_base_url=f"http://127.0.0.1:{server.server_port}/v1", llm_model="fixture")
    )
    try:
        started = time.monotonic()
        if phase == "normal":
            assert provider.generate("system", "user", {}, 2).content == "{}"
        else:
            with pytest.raises(ModelError, match="timeout"):
                provider.generate("system", "user", {}, 0.3)
            # Both slow responses take >1.5s to finish without cancellation. Allow
            # scheduling overhead while detecting a check made only after headers.
            assert time.monotonic() - started < 1.0
        assert received.is_set()
    finally:
        provider.close()
        stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
