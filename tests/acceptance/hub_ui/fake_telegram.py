# -*- coding: utf-8 -*-
"""最小假 Telegram（只在最外层用）：真网关进程启动与轮询够用的那几个调用。

- GET  /bot<token>/getMe     -> 一个 bot 身份（网关启动必须先拿到它）
- GET  /bot<token>/getUpdates -> 空更新（网关会按 poll_timeout_s 持续轮询）
- POST /bot<token>/<方法>     -> 一律 ok（测试不驱动 Telegram 侧，只让网关活着）

只绑 127.0.0.1，绝不发往 api.telegram.org。
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _route(self, method):
        """按方法名分派。网关的 Telegram 客户端两种方法都可能用（POST getUpdates 也合法），都处理。"""
        st = self.server.tg
        path = self.path.split("?")[0]
        st.hits.append((method, path))
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        if path.endswith("/getMe"):
            return self._json(200, {"ok": True, "result": {
                "id": st.bot_id, "is_bot": True, "username": st.username, "first_name": st.username}})
        if path.endswith("/getUpdates"):
            time.sleep(0.3)                      # 别让轮询转得太疯
            return self._json(200, {"ok": True, "result": []})
        if path.endswith("/sendMessage"):
            return self._json(200, {"ok": True, "result": {"message_id": 1, "chat": {"id": 1}, "date": 0, "text": ""}})
        return self._json(200, {"ok": True, "result": True})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


class FakeTelegram:
    def __init__(self, username="testbot", bot_id=123456, token="123456:TEST-token-0000000000000"):
        self.username = username
        self.bot_id = bot_id
        self.token = token
        self.hits = []
        self._srv = None
        self._port = None

    def start(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        srv.tg = self
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

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self._port
