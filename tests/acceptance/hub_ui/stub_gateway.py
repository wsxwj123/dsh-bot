# -*- coding: utf-8 -*-
"""假的新系统网关本机接口（管理台的下游替身，只在这一层用）。

按 INTERFACE-管理台UI 第 2、3 节的接口形状回应；记录收到的每个请求（路径、方法、body、口令对不对）。
三种坏模式：
- drop：读到请求就断连接（模拟"发出后中断"，契约要求管理台判成 504）；
- hang：挂着不回应（模拟"55 秒没回应"，测试里几乎不用，太重）；
- 不监听（端口直接没人听）用 free_port() 造，不用本类。

只绑 127.0.0.1，绝不发往外部网络。
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _read_body(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method):
        st = self.server.stub
        body = self._read_body()
        auth = self.headers.get("authorization") or ""
        rec = {
            "method": method,
            "path": self.path.split("?")[0],
            "body": body,
            "auth_ok": auth == "Bearer %s" % st.key,
            "auth_len": len(auth),
            "at": time.time(),
        }
        st.requests.append(rec)
        if st.mode == "drop":
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if st.mode == "hang":
            time.sleep(st.hang_seconds)
        if st.on_request:
            st.on_request()
        handler = st.routes.get(rec["path"])
        if handler is None:
            return self._json(404, {"error": "not found"})
        status, payload = handler(rec)
        return self._json(status, payload)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


class StubGateway:
    """可编程的假网关。默认所有路径 404。"""

    def __init__(self, key="test-api-key-0123456789"):
        self.key = key
        self.requests = []
        self.routes = {}
        self.mode = "ok"          # ok | drop | hang
        self.hang_seconds = 3.0
        self.on_request = None    # 每次收请求后的钩子（测试用来做"期间改动"）
        self._srv = None
        self._port = None

    def on(self, path, status, payload):
        """固定回应。"""
        self.routes[path] = lambda rec: (status, payload)
        return self

    def on_fn(self, path, fn):
        """按请求回应：fn(rec) -> (status, payload)。"""
        self.routes[path] = fn
        return self

    def start(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        srv.stub = self
        srv.daemon_threads = True
        self._srv = srv
        self._port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    @property
    def port(self):
        return self._port

    def hits(self, path):
        return [r for r in self.requests if r["path"] == path]
