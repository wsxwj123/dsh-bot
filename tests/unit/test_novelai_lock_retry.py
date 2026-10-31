"""NovelAI 并发锁 429 的排队重试：官方英文原文与中转站中文回法都要认作排队信号。"""
import http.server
import importlib.util
import json
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "novelai-skill" / "scripts" / "generate_novelai_image.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("generate_novelai_image", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _server(first_status: int, first_message: str):
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hits.append(1)
            if len(hits) == 1:
                body = json.dumps({"message": first_message, "statusCode": first_status}, ensure_ascii=False).encode()
                self.send_response(first_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"PK-fake")

        def log_message(self, *a):
            pass

    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, hits


@pytest.mark.parametrize("message", ["Concurrent generation is locked", "你有正在进行的任务，请等待完成后再试"])
def test_并发锁429等一等再试就成功(gen, message, monkeypatch):
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    s, hits = _server(429, message)
    try:
        body, err, _, failure = gen.request_with_retry(
            f"http://127.0.0.1:{s.server_port}/ai/generate-image", {"Content-Type": "application/json"}, {"x": 1},
            delays=(), lock_delays=(0,))
    finally:
        s.shutdown()
    assert body == b"PK-fake" and err is None and failure is None
    assert len(hits) == 2


@pytest.mark.parametrize("code,message", [(429, "rate limited"), (402, "Not enough Anlas")])
def test_其他错误不当排队信号_不重试(gen, code, message, monkeypatch):
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    s, hits = _server(code, message)
    try:
        body, _, _, failure = gen.request_with_retry(
            f"http://127.0.0.1:{s.server_port}/ai/generate-image", {"Content-Type": "application/json"}, {"x": 1},
            delays=(), lock_delays=(0,))
    finally:
        s.shutdown()
    assert body is None and failure == "other"
    assert len(hits) == 1
