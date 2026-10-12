# -*- coding: utf-8 -*-
"""bot 状态页：开关之后只动那一只（INTERFACE-新系统bot运维 §13a，第 6 稿追加）。

验收测试按页面源码的函数名与固定片段断言，测不到「点了之后到底发了哪些请求、哪句话
在什么时候显示」。这里把模板里的那段页面脚本原样交给 node 跑一遍：浏览器的几样东西
（document、hubApi、hubSay、confirm、setTimeout）换成记账的替身，后端的响应由用例给，
量出来的是脚本自己的逻辑。机器上没有 node 时这一部分跳过，文件末尾另有几条不靠 node 的
源码断言兜底。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "moments" / "templates" / "hub_bots.html"
NODE = shutil.which("node")
GATE_404 = "没有这个新系统的 bot（这一页只管新系统的 bot）"
RESTART_ALL = "POST /hub/api/bots/restart"

#: 驱动脚本：取模板里第一段 <script>，在替身环境里执行 scenario 指定的那个按钮处理器，
#: 把确认框文案、发出的请求、写进 #restart-msg 的话按顺序吐成一行 JSON。
DRIVER = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const log = {confirms: [], requests: [], says: []};
const els = {};
function el(id) {
  return els[id] || (els[id] = {id: id, style: {}, disabled: false, innerHTML: '',
                                textContent: '', addEventListener() {}, appendChild() {}});
}
let seq = 0;
const doc = {getElementById: el, createElement: () => el('made-' + (seq++))};
function hubSay(node, text) { if (node && node.id === 'restart-msg') log.says.push(text); }
let polls = 0;
async function hubApi(url, opts) {
  log.requests.push(((opts && opts.method) || 'GET') + ' ' + url);
  if (url === '/hub/api/bots') {
    return {status: 200, body: {bots: [], disabled: [], sources: {}, overrides: [],
                                restart_available: true}};
  }
  if (url.startsWith('/hub/api/bots/restart/')) {
    const list = scenario.polls || [];
    return list[Math.min(polls++, list.length - 1)] || {status: 404, body: {}};
  }
  return scenario.reply;
}
const timers = [];
const boot = new Function('document', 'hubApi', 'hubSay', 'confirm', 'setTimeout',
  script + '\nreturn {setEnabled, restartOne, startRestart};');
const page = boot(doc, hubApi, hubSay,
                  (text) => { log.confirms.push(text); return scenario.confirm !== false; },
                  (fn) => { timers.push(fn); });
const tick = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  for (let i = 0; i < 5; i++) await tick();        // 页面加载时那次 loadBots() 先走完
  log.requests.length = 0;
  const btn = {disabled: false};
  if (scenario.act === 'restartOne') await page.restartOne(scenario.bot, btn);
  else if (scenario.act === 'startRestart') await page.startRestart();
  else await page.setEnabled(scenario.bot, scenario.enabled, btn);
  for (let i = 0; i < 40; i++) {                   // 轮询的定时器逐个放行，直到没有新的
    await tick();
    const fn = timers.shift();
    if (fn) await fn();
  }
  log.btnDisabled = btn.disabled;
  console.log(JSON.stringify(log));
})();
"""

needs_node = pytest.mark.skipif(NODE is None, reason="这台机器没有 node，页面脚本跑不起来")


def _run(tmp_path, **scenario):
    scenario.setdefault("bot", "xbot")
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER, encoding="utf-8")
    spec = tmp_path / "scenario.json"
    spec.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([NODE, str(driver), str(TEMPLATE), str(spec)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout.decode("utf-8"))


def _ok(sync):
    body = {"ok": True, "enabled": False, "restart_hint": {"bot": True, "moments_web": False}}
    if sync is not None:
        body["sync"] = sync
    return {"status": 200, "body": body}


def _job(state, tail="", exit_code=None):
    return {"status": 200, "body": {"state": state, "exit_code": exit_code, "took_ms": 1,
                                    "tail": tail}}


STARTED = {"state": "started", "job_id": "abcd1234", "started_at": 1}
POLL = "GET /hub/api/bots/restart/abcd1234"
LIST = "GET /hub/api/bots"


# ------------------------------------------------------------ 确认框只说这一只

@needs_node
def test_停止的确认框只说这一只_取消则一个请求都不发(tmp_path):
    log = _run(tmp_path, enabled=False, confirm=False, reply=_ok(STARTED))
    assert log["confirms"] == ["停止 xbot？只停这一只，别的 bot 不受影响；它正在处理的对话会被打断。"]
    assert log["requests"] == [] and log["says"] == []


@needs_node
def test_启用的确认框只说这一只(tmp_path):
    log = _run(tmp_path, enabled=True, confirm=False, reply=_ok(STARTED))
    assert log["confirms"] == ["启用 xbot？只启动这一只，别的 bot 不受影响。"]


# ------------------------------------------------------------ started：只轮询那一个任务

@needs_node
def test_started_只轮询那一个job_不发重启全部_终态接tail末行(tmp_path):
    log = _run(tmp_path, enabled=False, reply=_ok(STARTED),
               polls=[_job("running"), _job("done", "重启 1 只：成功 1\nxbot 已停止\n")])
    assert log["requests"] == ["POST /hub/api/bots/xbot/enabled", LIST, POLL, POLL, LIST]
    assert RESTART_ALL not in log["requests"]
    assert log["says"] == ["xbot 的配置已改成停用，正在同步这一只…", "正在同步 xbot…",
                           "同步完成：xbot 已停止"]
    assert log["btnDisabled"] is False


@needs_node
def test_started_启用方向失败_显示同步失败加tail末行(tmp_path):
    fail = "xbot 失败：45 秒内没有新心跳；日志 /x/bots/xbot/logs/launchd.log"
    log = _run(tmp_path, enabled=True, reply=_ok(STARTED),
               polls=[_job("failed", "重启 1 只：成功 0，失败 1\n" + fail)])
    assert log["says"] == ["xbot 的配置已改成启用，正在同步这一只…", "同步失败：" + fail]
    assert RESTART_ALL not in log["requests"]


@needs_node
def test_started_超时且tail为空_只显示同步超时_不带冒号(tmp_path):
    log = _run(tmp_path, enabled=False, reply=_ok(STARTED), polls=[_job("timeout", "")])
    assert log["says"][-1] == "同步超时"


@needs_node
def test_started_job状态读不到_沿用既有那句(tmp_path):
    log = _run(tmp_path, enabled=False, reply=_ok(STARTED), polls=[{"status": 404, "body": {}}])
    assert log["says"][-1] == "任务状态读不到了"
    assert log["requests"].count(POLL) == 1


# ------------------------------------------------------------ busy / skipped：不轮询，不再发请求

@needs_node
def test_busy_说明这一只还没同步_不轮询不重启(tmp_path):
    log = _run(tmp_path, enabled=False, reply=_ok({"state": "busy", "job_id": "abcd1234"}))
    assert log["says"] == ["xbot 的配置已改成停用，但这一只还没同步：有别的重启任务正在跑。"
                           "等它结束后点「重启全部 bot」补上。"]
    assert log["requests"] == ["POST /hub/api/bots/xbot/enabled", LIST]


@needs_node
def test_skipped_说明没有自动同步_不轮询不重启(tmp_path):
    log = _run(tmp_path, enabled=True, reply=_ok({"state": "skipped", "reason": "no_launchd"}))
    assert log["says"] == ["xbot 的配置已改成启用，但没有自动同步：这台机器不是 macOS，"
                           "管理台没法替你启停它，请手动处理。"]
    assert log["requests"] == ["POST /hub/api/bots/xbot/enabled", LIST]


@needs_node
@pytest.mark.parametrize("sync", [None, {"state": "whatever"}, {"state": "started"}],
                         ids=["no-sync", "unknown-state", "started-without-job"])
def test_响应里的sync认不出来_给一句提示_不抛错也不发重启(tmp_path, sync):
    log = _run(tmp_path, enabled=False, reply=_ok(sync))
    assert len(log["says"]) == 1 and log["says"][0].startswith("xbot 的配置已改成停用")
    assert log["requests"] == ["POST /hub/api/bots/xbot/enabled", LIST]


# ------------------------------------------------------------ 失败：沿用既有口径

@needs_node
def test_开关被拒_显示失败那一句_按钮恢复_没有后续请求(tmp_path):
    log = _run(tmp_path, enabled=False,
               reply={"status": 404, "body": {"error": "bot_not_found", "detail": GATE_404}})
    assert log["says"] == ["停止失败：" + GATE_404]
    assert log["requests"] == ["POST /hub/api/bots/xbot/enabled"]
    assert log["btnDisabled"] is False


@needs_node
def test_开关失败_没有detail就用error_再没有就用状态码(tmp_path):
    log = _run(tmp_path, enabled=True, reply={"status": 409, "body": {"error": "bad_yaml"}})
    assert log["says"] == ["启用失败：bad_yaml"]
    log = _run(tmp_path, enabled=True, reply={"status": 500, "body": {}})
    assert log["says"] == ["启用失败：HTTP 500"]


# ------------------------------------------------------------ 没改的那两个入口照旧

@needs_node
def test_行内重启_文案与轮询的说法一字不变(tmp_path):
    log = _run(tmp_path, act="restartOne",
               reply={"status": 202, "body": {"job_id": "abcd1234", "started_at": 1}},
               polls=[_job("running"), _job("done", "重启 1 只：成功 1\nxbot 已重启")])
    assert log["confirms"] == ["重启 xbot？正在处理的对话会被打断。"]
    assert log["says"] == ["正在重启 xbot…", "重启中…", "重启结束：done"]


@needs_node
def test_重启全部_轮询的说法一字不变_覆盖命令的退出码照旧带出(tmp_path):
    log = _run(tmp_path, act="startRestart",
               reply={"status": 202, "body": {"job_id": "abcd1234", "started_at": 1}},
               polls=[_job("running"), _job("failed", "boom", exit_code=3)])
    assert log["requests"][0] == RESTART_ALL
    assert log["says"] == ["重启中…", "重启结束：failed（退出码 3）"]


# ------------------------------------------------------------ 不靠 node 的源码兜底

def _body(html, name):
    """§13a 的切法：从该函数的 function 那一行起，到后面第一个顶层函数定义之前。"""
    tops = [(m.start(), m.group(1)) for m in re.finditer(
        r"^(?:async[ \t]+)?function[ \t]+([A-Za-z_$][\w$]*)[ \t]*\(", html, re.M)]
    for i, (pos, fn) in enumerate(tops):
        if fn == name:
            return html[pos:(tops[i + 1][0] if i + 1 < len(tops) else len(html))]
    raise AssertionError("页面里没有顶层函数 %s" % name)


def test_源码_开关成功后先重拉列表再交给showSync_不调重启全部():
    body = _body(TEMPLATE.read_text(encoding="utf-8"), "setEnabled")
    assert "startRestart(" not in body
    assert 0 < body.index("loadBots()") < body.index("showSync(")


def test_源码_轮询再调用时把label带上_另两个入口不带():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "pollJob(jobId, label)" in _body(html, "pollJob")
    assert "pollJob(sync.job_id, botId)" in _body(html, "showSync")
    assert "pollJob(b.job_id);" in _body(html, "restartOne")
    assert "pollJob(r.body.job_id);" in _body(html, "startRestart")
