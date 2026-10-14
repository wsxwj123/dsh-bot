# -*- coding: utf-8 -*-
"""适配服务的请求体上限对分块传输同样有效（安审 S4）。

分块传输的请求没有 Content-Length，原来的上限只看这个头：框架会先把整个请求体读进内存再比长度。
这里直接拿 WSGI 环境调应用，摆成服务器处理分块请求时给的样子（没有 ``CONTENT_LENGTH``、
``wsgi.input_terminated`` 为真），请求体是一个会记账的流。看三件事：超了回 422 invalid；读进来的
字节数封顶在上限，不把整个流读完；正好在上限上的照常往下走。
"""
import io
import json

import pytest
from werkzeug.test import EnvironBuilder

from moments import adapter_service as svc

TOKEN = "unit-adapter-token"
HEAD = b'{"effort": "high"}'        # 合规的 JSON 对象；后面补空格，整体仍是合法 JSON


class _Counting(io.RawIOBase):
    """``size`` 个字节的请求体：开头是 HEAD，后面全是空格。``served`` 是被读走的字节数。"""

    def __init__(self, size):
        self.data, self.served = HEAD + b" " * (size - len(HEAD)), 0

    def readable(self):
        return True

    def readinto(self, b):
        chunk = self.data[self.served:self.served + len(b)]
        b[:len(chunk)] = chunk
        self.served += len(chunk)
        return len(chunk)


@pytest.fixture
def send(tmp_path, monkeypatch):
    token_file = tmp_path / "adapter.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))
    app = svc.create_app()

    def _send(stream):
        env = EnvironBuilder(path="/adapter/v1/bots/botalpha/provider", method="POST",
                             headers={"Authorization": "Bearer " + TOKEN,
                                      "Content-Type": "application/json",
                                      "Transfer-Encoding": "chunked"}).get_environ()
        env.pop("CONTENT_LENGTH", None)
        env["wsgi.input"], env["wsgi.input_terminated"] = stream, True
        got = {}

        def start_response(status, headers, exc_info=None):
            got["status"] = int(status.split()[0])
        body = b"".join(app(env, start_response))
        return got["status"], json.loads(body)
    return _send


@pytest.mark.parametrize("size", [svc.MAX_BODY + 1, 1024 * 1024], ids=["多一个字节", "1MB"])
def test_分块传输超了上限_回422_读进来的封顶在上限(send, size):
    stream = _Counting(size)
    status, got = send(stream)
    assert (status, got["code"]) == (422, "invalid"), got
    assert stream.served <= svc.MAX_BODY + 1, "把超长的请求体整个读进来了：%d 字节" % stream.served


def test_分块传输正好在上限上_照常往下走(send):
    status, got = send(_Counting(svc.MAX_BODY))
    assert (status, got["code"]) == (404, "unknown_bot"), "请求体合规，应走到找 bot 这一步：%r" % got
