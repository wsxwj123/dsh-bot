# -*- coding: utf-8 -*-
"""旧系统供应商入口验收的工具件：约定里的常量、发请求的小函数、探路由、旁观记录。

只依据 `.devflow/INTERFACE-旧系统供应商入口.md`，不读实现。这里出现的名字全部来自约定：
两个配置项、两个默认地址、五个转发头、十五条已下线的接口、六个不许再加载的模块。
"""
import contextlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.datastructures import Headers
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import RequestRedirect
from werkzeug.test import EnvironBuilder, run_wsgi_app

# ------------------------------------------------------------ §1 配置项

LOCAL_ENV = "HUB_LEGACY_PROVIDER_URL_LOCAL"
PUBLIC_ENV = "HUB_LEGACY_PROVIDER_URL_PUBLIC"
DEFAULT_LOCAL = "http://127.0.0.1:8767/provider"
DEFAULT_PUBLIC = "https://legacy-provider.invalid/provider"
DEFAULT_OF = {LOCAL_ENV: DEFAULT_LOCAL, PUBLIC_ENV: DEFAULT_PUBLIC}

#: §1「可断言」点名的六个不合法值
BAD_VALUES_FROM_SPEC = [
    pytest.param("javascript:alert(1)", id="javascript-scheme"),      # javascript 伪协议
    pytest.param("//evil.example/x", id="scheme-relative"),           # 没写协议的双斜杠
    pytest.param("ftp://a.example/x", id="ftp-scheme"),               # ftp 协议
    pytest.param("https://a b.example/x", id="space-in-host"),        # 主机名里有空格
    pytest.param("https://u:p@a.example/x", id="userinfo"),           # 带用户名口令
    pytest.param("https:///x", id="empty-host"),                      # 没有主机名
]

# ------------------------------------------------------------ §2 判据

#: 一个符合「本机入口」第 1 条的 Host，与一个不符合的
LOCAL_HOST = "127.0.0.1:8765"
PUBLIC_HOST = "8765.example.com"

#: §2 第 2 条点名的五个转发头，各配一个像真的值
FORWARD_HEADERS = [
    ("X-Forwarded-For", "203.0.113.9"),
    ("X-Forwarded-Proto", "https"),
    ("X-Forwarded-Host", "8765.example.com"),
    ("Forwarded", "for=203.0.113.9;proto=https"),
    ("CF-Connecting-IP", "203.0.113.9"),
]

# ------------------------------------------------------------ §3 导航与卡片

NAV_PAGES = ["/hub", "/hub/bots", "/hub/addbot", "/hub/dsh-model", "/hub/setup", "/hub/params",
             "/hub/provider"]
NAV_ANCHOR = ('<a class="chip" href="/provider" target="_blank" rel="noopener noreferrer">'
              '旧系统供应商</a>')
CARD_OPEN_TAG = '<a class="card cardlink" href="/provider" target="_blank" rel="noopener noreferrer">'
CARD_TEXT = "打开旧系统那边自己的供应商页（新标签页）。这里只是入口，页面与数据都在那边。"
CARD_OLD_TEXT = "原来的 /provider 页面"

# ------------------------------------------------------------ §5 已下线的十五条

#: 假名。绝不用本机真实在用的 bot 名去碰这些路径。
FAKE_BOT = "jiabot"
FAKE_PROVIDER = "jia-gys-001"

#: (原方法, 约定里的写法, 发请求用的具体路径, 原来做什么)
RETIRED = [
    ("GET", "/api/providers", "/api/providers", "概览"),
    ("POST", "/api/providers", "/api/providers", "新建供应商"),
    ("PUT", "/api/providers/<provider_id>", "/api/providers/%s" % FAKE_PROVIDER, "改供应商"),
    ("DELETE", "/api/providers/<provider_id>", "/api/providers/%s" % FAKE_PROVIDER, "删供应商"),
    ("POST", "/api/providers/models", "/api/providers/models", "拉模型并存下"),
    ("POST", "/api/providers/sync_all", "/api/providers/sync_all", "给所有bot重新套用并排队重启"),
    ("GET", "/api/aux-llm", "/api/aux-llm", "读辅助模型设置"),
    ("POST", "/api/aux-llm", "/api/aux-llm", "写辅助模型设置"),
    ("POST", "/api/bots/<bot_id>/provider", "/api/bots/%s/provider" % FAKE_BOT, "给bot切供应商并重启"),
    ("POST", "/api/bots/<bot_id>/provider/reset", "/api/bots/%s/provider/reset" % FAKE_BOT,
     "清掉bot的供应商绑定并重启"),
    ("POST", "/api/bots/<bot_id>/restart", "/api/bots/%s/restart" % FAKE_BOT, "手动重启bot"),
    ("GET", "/api/proxy", "/api/proxy", "代理状态"),
    ("GET", "/api/proxy/generation", "/api/proxy/generation", "代理代号"),
    ("POST", "/api/proxy/restart", "/api/proxy/restart", "重启代理进程"),
    ("POST", "/api/proxy/sync", "/api/proxy/sync", "重写代理配置并重载"),
]
assert len(RETIRED) == 15, "约定 §5 是十五条"

#: 用例编号只用英文字符（方法 + 约定里的写法），中文编号在终端里会被转成一串 \\u 码
RETIRED_PARAMS = [pytest.param(m, p, id="%s_%s" % (m, spec)) for m, spec, p, _why in RETIRED]
RETIRED_PATHS = sorted({p for _, _, p, _ in RETIRED})
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]

#: 旧页面与旧接口的响应上原有、现在不许再带的三个头（§5）
LEGACY_ONLY_HEADERS = ["X-Proxy-Generation", "X-Frame-Options", "Content-Security-Policy"]

#: 一个像真的请求体。下线之后它不该被任何人读到。
PLAUSIBLE_BODY = {"name": "假供应商", "base_url": "https://api.example.test/v1",
                  "api_key": "sk-test-0000", "models": ["m1"], "provider_id": FAKE_PROVIDER,
                  "model": "m1"}

#: 预置在「旧系统那边」的两份文件的原文。任何用例跑完它们都该一个字节不差。
SEED_PROVIDERS_JSON = json.dumps({
    "providers": [{"id": FAKE_PROVIDER, "name": "假供应商",
                   "base_url": "https://api.example.test/v1", "api_key": "sk-test-0000",
                   "models": ["m1"], "protocol": "anthropic"}],
    "bindings": {FAKE_BOT: {"provider_id": FAKE_PROVIDER, "model": "m1"}},
}, ensure_ascii=False, indent=1).encode("utf-8")

SEED_GLOBAL_YML = (
    "# 旧系统全局配置（测试预置，一个字节都不许被改）\n"
    "user_display_name: 测试\n"
    "max_calls_per_5h: 100\n"
    "moments:\n"
    "  enabled: true\n"
    "jiwen:\n"
    "  tick_interval_min: 5\n"
).encode("utf-8")

# ------------------------------------------------------------ §6 导入面

RETIRED_MODULES = ["moments.provider_routes", "provider_config", "provider_proxy",
                   "provider_paths", "legacy_config_loader", "scripts.restart_bot_worker"]
RETIRED_BLUEPRINT = "provider"


# ------------------------------------------------------------ 发请求

def go(client, path="/provider", host=LOCAL_HOST, headers=None, method="GET", **kw):
    """发一次请求，绝不跟随跳转。`host` 原样写进 Host 头；给 None 就用测试客户端默认的。"""
    over = dict(kw.pop("environ_overrides", None) or {})
    if host is not None:
        over["HTTP_HOST"] = host
    return client.open(path, method=method, headers=headers or {}, environ_overrides=over,
                       follow_redirects=False, **kw)


def call(client, method, path, **kw):
    """对一条旧接口发一次请求。写类方法带一个像真的 JSON 体。调用前必须先过 require_retired。"""
    if method in ("POST", "PUT", "PATCH"):
        kw.setdefault("json", PLAUSIBLE_BODY)
    return go(client, path=path, method=method, **kw)


def location(resp):
    """这次响应把人带去哪里。不是 302 就当场失败，并把实得的状态码报出来。"""
    assert resp.status_code == 302, "期望 302 跳转，实得 %s" % resp.status_code
    return resp.headers.get("Location")


def wsgi_get(app, path="/provider", drop_host=False, **environ_set):
    """绕过测试客户端直接按 WSGI 调一次应用，返回 (状态码, 响应头, 响应体字节)。

    测试客户端总会替请求补一个 Host 头，量「没有 Host」只能走这里。
    """
    environ = EnvironBuilder(path=path, method="GET").get_environ()
    if drop_host:
        environ.pop("HTTP_HOST", None)
    environ.update(environ_set)
    app_iter, status, headers = run_wsgi_app(app, environ, buffered=True)
    try:
        body = b"".join(app_iter)
    finally:
        close = getattr(app_iter, "close", None)
        if callable(close):
            close()
    return int(status.split()[0]), Headers(headers), body


def cache_directives(resp_or_headers):
    """把 Cache-Control 拆成小写指令列表。"""
    headers = getattr(resp_or_headers, "headers", resp_or_headers)
    return [d.strip().lower() for d in (headers.get("Cache-Control") or "").split(",") if d.strip()]


def write_env_file(path, **pairs):
    """按 hub.env 的写法落一份「键=值」文件。"""
    Path(path).write_text("# 测试用\n" + "".join("%s=%s\n" % kv for kv in pairs.items()),
                          encoding="utf-8")
    return path


# ------------------------------------------------------------ 探路由（§6 第 1 条）

def route_state(app, path):
    """路由清单里还有没有规则能匹配这条路径：'gone' 一条都没有；'alive' 有（不论它收哪种方法）。"""
    adapter = app.url_map.bind("localhost")
    try:
        adapter.match(path, method="GET")
    except NotFound:
        return "gone"
    except (MethodNotAllowed, RequestRedirect):
        return "alive"
    return "alive"


def require_retired(app, path):
    """旧接口还在路由清单里就当场判失败，**不对它发请求**。

    这十五条在下线之前是活的：有的会杀进程重拉、有的会调 launchctl、有的会写旧系统的
    供应商清单。量「它已经下线」的用例如果先把请求发出去，等于拿活着的接口做实验。
    所以先看路由清单，清单里没有了才发。
    """
    if route_state(app, path) == "alive":
        pytest.fail("功能还没实现：路由清单里还有能匹配 %s 的规则。旧接口活着的时候会重启进程、"
                    "写旧系统的清单，为安全起见这条用例没有对它发请求。" % path, pytrace=False)


def rules_of(app, rule_text):
    """路由清单里写法逐字等于 rule_text 的规则。"""
    return [r for r in app.url_map.iter_rules() if r.rule == rule_text]


# ------------------------------------------------------------ 旁观记录（§6 第 3 条）

#: 会起子进程、发信号的审计事件。subprocess.run / call / check_output、os.popen 最后都落到
#: subprocess.Popen 上；别的几种是绕开 subprocess 的写法。
_PROCESS_EVENTS = frozenset((
    "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec",
    "os.spawn", "os.startfile", "pty.spawn", "os.kill", "os.killpg"))
_AUDIT_ATTR = "_dsh_legacy_entry_audit"


def _brief(args):
    """只留前两个参数（可执行文件与参数表、或进程号与信号）。子进程事件的第四个参数是整份
    环境变量，里面可能有口令，不能跟着失败信息打到终端上。"""
    try:
        return repr(tuple(args[:2]))[:300]
    except Exception:                                   # noqa: BLE001  审计钩子里不许抛
        return "<参数打印不出来>"


def _audit_sinks():
    """进程里只装一次审计钩子（装了卸不掉）。没有人在旁观时它什么都不记。"""
    sinks = getattr(sys, _AUDIT_ATTR, None)
    if sinks is None:
        sinks = []

        def _hook(event, args):
            if sinks and event in _PROCESS_EVENTS:
                rec = "%s %s" % (event, _brief(args))
                for sink in list(sinks):
                    sink.append(rec)

        sys.addaudithook(_hook)
        setattr(sys, _AUDIT_ATTR, sinks)
    return sinks


def snapshot(*roots):
    """把几棵目录树拍成 {路径: 字节}（目录记成 None）。只用在临时目录上。"""
    out = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            out[str(root)] = "不存在"
            continue
        out[str(root)] = None
        for p in sorted(root.rglob("*")):
            out[str(p)] = None if p.is_dir() else p.read_bytes()
    return out


def snapshot_diff(before, after):
    """两次快照的差别，写成人能看懂的几行；一样就是空列表。"""
    lines = []
    for path in sorted(set(before) | set(after)):
        if path not in after:
            lines.append("少了 %s" % path)
        elif path not in before:
            lines.append("多了 %s" % path)
        elif before[path] != after[path]:
            lines.append("内容变了 %s" % path)
    return lines


class Watched:
    """一段时间里旁观到的四样东西。"""

    def __init__(self):
        self.processes = []     # 起子进程、发信号的审计事件
        self.threads = []       # 这段时间里被启动的线程名
        self.launchctl = []     # 假 launchctl 收到的调用（每次一组参数）
        self.legacy_diff = []   # 旧系统那边的文件有什么变化


@contextlib.contextmanager
def watching(sandbox):
    """旁观 with 块里的全部动静：子进程与信号、新起的线程、假 launchctl、旧系统那边的文件。"""
    seen = Watched()
    sinks = _audit_sinks()
    calls_before = len(sandbox.launchctl_calls())
    files_before = snapshot(*sandbox.legacy_roots)
    orig_start = threading.Thread.start

    def _start(thread, *a, **k):
        seen.threads.append(thread.name)
        return orig_start(thread, *a, **k)

    threading.Thread.start = _start
    sinks.append(seen.processes)
    try:
        yield seen
    finally:
        sinks.remove(seen.processes)
        threading.Thread.start = orig_start
        seen.launchctl = sandbox.launchctl_calls()[calls_before:]
        seen.legacy_diff = snapshot_diff(files_before, snapshot(*sandbox.legacy_roots))


# ------------------------------------------------------------ 记账的假 launchctl

_FAKE_LAUNCHCTL_PY = '''\
# -*- coding: utf-8 -*-
"""旧系统供应商入口验收用的假 launchctl：只记账，绝不转发给真实命令。

只读的 print 照真 launchctl 的样子报找不到服务（退 113）；别的子命令一律退 97。
"""
import json
import sys

log, argv = sys.argv[1], sys.argv[2:]
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(argv, ensure_ascii=False) + "\\n")
if argv[:1] == ["print"]:
    sys.stderr.write("Could not find service in domain for user gui\\n")
    sys.exit(113)
sys.exit(97)
'''


def make_recording_launchctl(dirpath):
    """在 dirpath 下造一支记账的假 launchctl，返回 (可执行文件路径, 记账文件路径)。"""
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    script = d / "fake_launchctl.py"
    log = d / "calls.jsonl"
    script.write_text(_FAKE_LAUNCHCTL_PY, encoding="utf-8")
    if os.name == "nt":
        exe = d / "launchctl.cmd"
        exe.write_text('@"%s" "%s" "%s" %%*\r\n' % (sys.executable, script, log), encoding="utf-8")
    else:
        exe = d / "launchctl"
        exe.write_text('#!/bin/sh\nexec "%s" "%s" "%s" "$@"\n' % (sys.executable, script, log),
                       encoding="utf-8")
        exe.chmod(0o755)
    return exe, log


def read_launchctl_calls(log):
    log = Path(log)
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
