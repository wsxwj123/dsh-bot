# -*- coding: utf-8 -*-
"""调网关不跟跳转（安审 S1）。

适配服务与管理台到网关的请求都带网关口令。默认的跳转处理会把口令头原样带到 Location 指的地方：
端口上要是别的本机进程，回一个跳转就把口令引走了。这里起两个只绑 127.0.0.1 的假件：一个冒充
网关、一律回跳转指向另一个；断言另一个一次都没被请求（口令自然没被带过去），调用方拿到的是那个
3xx 状态码。
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import gateway_client

KEY = "unit-gateway-key-must-not-follow"


class _Server:
    """只绑 127.0.0.1 的假件：记下每个请求 ``(方法, 路径, 请求头)``，一律回同一个应答。"""

    def __init__(self, status, headers=None, body=b'{"ok": true}'):
        self.hits = []
        hits = self.hits

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("content-length") or 0)
                if n:
                    self.rfile.read(n)
                hits.append((self.command, self.path, dict(self.headers)))
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _any

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        self.port = self._srv.server_address[1]

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def redirecting(tmp_path, request):
    """``(频道目录, 冒充网关的假件, 跳转目标)``。状态码由用例参数给。"""
    target = _Server(200)
    gateway = _Server(request.param,
                      headers={"Location": "http://127.0.0.1:%d/v1/model" % target.port})
    state, channel = tmp_path / "bot" / "state", tmp_path / "bot" / "channel"
    state.mkdir(parents=True)
    channel.mkdir()
    (state / "api.key").write_text(KEY + "\n", encoding="utf-8")
    (state / "api.port").write_text("%d\n" % gateway.port, encoding="utf-8")
    yield str(channel), gateway, target
    gateway.stop()
    target.stop()


@pytest.mark.parametrize("redirecting", [301, 302, 303, 307], indirect=True)
@pytest.mark.parametrize("method,path,body", [("GET", "/v1/model", None),
                                              ("POST", "/v1/model", {"spec": "a/b"})],
                         ids=["读", "写"])
def test_网关回跳转_不跟_口令不带过去_调用方拿到跳转的状态码(redirecting, method, path, body):
    channel, gateway, target = redirecting
    code, _got = gateway_client._call(channel, method, path, body, timeout=5)
    assert target.hits == [], "跟着跳转把请求（连同网关口令）发到了别处：%r" % target.hits
    assert len(gateway.hits) == 1
    assert 300 <= code < 400, code
