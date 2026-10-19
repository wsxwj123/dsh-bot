# -*- coding: utf-8 -*-
"""适配服务验收：新增的 6 个页面服务接口（/hub/api/oauth/*）。

依据只有 `.devflow/adapter-oauth-routes-接口.md` 的行为清单（6 条）与错误契约。
只经 HTTP 入口（create_app() 的测试客户端，即 adapter 夹具）发请求，不 import 内部模块、
不 mock 内部层、不断言内部状态。
"""
import json

from .adapter_util import adapter, console_guard, NO_BODY  # noqa: F401  夹具与"不带请求体"哨兵

PREFIX = "/hub/api"
ACCOUNTS = PREFIX + "/oauth/accounts"
START = PREFIX + "/oauth/start"
SUBMIT = PREFIX + "/oauth/submit"
STATUS = PREFIX + "/oauth/status"

#: 空对象请求体（activate 契约里写的就是 `{}`）
EMPTY = {}


def _body(resp):
    """把响应体当 JSON 对象解析；不是 JSON 对象就带着状态码与开头内容报错。"""
    raw = resp.get_data(as_text=True)
    try:
        body = json.loads(raw)
    except ValueError:
        raise AssertionError("响应体不是 JSON：状态 %s，开头 %r" % (resp.status_code, raw[:200]))
    assert isinstance(body, dict), "响应体不是 JSON 对象：状态 %s，%r" % (resp.status_code, body)
    return body


def _assert_error(resp, status, code):
    """错误契约：状态码 + `error` 机器码 + 非空 `detail` 文案。"""
    body = _body(resp)
    assert resp.status_code == status, \
        "应为 %d，实际 %d：%r" % (status, resp.status_code, body)
    assert body.get("error") == code, \
        "error 应为 %r，实际 %r（响应体 %r）" % (code, body.get("error"), body)
    detail = body.get("detail")
    assert isinstance(detail, str) and detail.strip(), \
        "detail 应是非空文案，实际 %r（响应体 %r）" % (detail, body)


def test_行为1_带令牌取账号列表_200且顶层含accounts与warnings(adapter):
    """行为清单第 1 条。依赖（cliproxy）未配置：仍 200，accounts 为空、warnings 非空。"""
    resp = adapter.call("GET", ACCOUNTS)
    body = _body(resp)
    assert resp.status_code == 200, "应为 200，实际 %d：%r" % (resp.status_code, body)
    assert "accounts" in body, "顶层缺少 accounts：%r" % (body,)
    assert "warnings" in body, "顶层缺少 warnings：%r" % (body,)
    assert isinstance(body["accounts"], list), "accounts 应是列表，实际 %r" % (body["accounts"],)
    assert isinstance(body["warnings"], list), "warnings 应是列表，实际 %r" % (body["warnings"],)
    assert body["accounts"] == [], "依赖未配置时 accounts 应为空，实际 %r" % (body["accounts"],)
    assert body["warnings"], "依赖未配置时 warnings 应非空，实际 %r" % (body["warnings"],)


def test_行为2_带令牌start不认识的provider_400_bad_provider(adapter):
    """行为清单第 2 条。"""
    resp = adapter.call("POST", START, {"provider": "nosuch"})
    _assert_error(resp, 400, "bad_provider")


def test_行为3_带令牌status缺state_400_bad_body(adapter):
    """行为清单第 3 条。"""
    resp = adapter.call("GET", STATUS)
    _assert_error(resp, 400, "bad_body")


def test_行为4_带令牌POST到accounts路径_405且Allow含GET(adapter):
    """行为清单第 4 条。"""
    resp = adapter.call("POST", ACCOUNTS, EMPTY)
    assert resp.status_code == 405, \
        "应为 405，实际 %d：%r" % (resp.status_code, resp.get_data(as_text=True)[:200])
    allow = resp.headers.get("Allow")
    assert allow is not None, "405 响应缺少 Allow 头"
    methods = {m.strip().upper() for m in allow.split(",")}
    assert "GET" in methods, "Allow 头应包含 GET，实际 %r" % (allow,)


#: 6 个页面服务接口各一个"本来合规"的无令牌请求：(方法, 路径, 请求体)
SIX_WITHOUT_TOKEN = [
    ("GET", ACCOUNTS, NO_BODY),
    ("POST", START, {"provider": "codex"}),
    ("POST", SUBMIT, {"provider": "codex", "code": "abc", "state": "xyz"}),
    ("GET", STATUS + "?state=xyz", NO_BODY),
    ("POST", ACCOUNTS + "/codex/deadbeef/activate", EMPTY),
    ("DELETE", ACCOUNTS + "/codex/deadbeef", NO_BODY),
]


def test_行为5_不带令牌请求六个接口_全部401(adapter):
    """行为清单第 5 条。"""
    failures = []
    for method, path, body in SIX_WITHOUT_TOKEN:
        resp = adapter.call(method, path, body, auth=None)
        if resp.status_code != 401:
            failures.append("%s %s → %d（应 401）：%r"
                            % (method, path, resp.status_code, resp.get_data(as_text=True)[:200]))
    assert not failures, "这些请求没回 401：\n" + "\n".join(failures)


def test_行为6_带令牌未知路径404_not_found且既有bots路径照常200(adapter):
    """行为清单第 6 条。"""
    unknown = adapter.call("GET", PREFIX + "/nosuch")
    _assert_error(unknown, 404, "not_found")

    existing = adapter.call("GET", "/hub/bots")
    assert existing.status_code == 200, \
        "既有路径 /hub/bots 应照常 200，实际 %d：%r" % (
            existing.status_code, existing.get_data(as_text=True)[:200])
