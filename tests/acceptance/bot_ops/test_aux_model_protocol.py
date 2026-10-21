# -*- coding: utf-8 -*-
"""aux-model-protocol 验收测试（黑盒，按供应商级别判定协议）。

依据：`.devflow/aux-model-protocol-接口.md` 的行为清单，一条行为一个用例。

只走对外接口：
  * 每个用例起一个干净子进程，调用公开入口 `moments.post.maybe_post_moment`
    （朋友圈生成的公开入口），测试进程不 import 产品模块。
  * providers.json / _global.yml 写到临时目录，通过三个接缝环境变量让实现读取。
  * 上游用本机回环假 HTTP 服务，只记录请求路径；原 DeepSeek 路径用假 CLI 记录调用。
  * HOME 等全部钉在 pytest 临时目录，不连外网、不碰真实配置。

判定口径（新）：协议只看供应商级别。
  1. 供应商 `protocol` 字段；
  2. 未写则看 `base_url` 路径里有没有 `anthropic` 段；
  3. 再看有没有 `v1` 段 → openai；
  4. 都没有 → 默认 openai。
逐模型的 `protocols` 标注不参与判定。

断言口径：假上游实际收到的请求路径（openai 为 /v1/chat/completions，
anthropic 为 /v1/messages）、假 CLI 调用次数、落库的朋友圈正文。
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="假 LLM 是 sh 脚本，Windows 上整文件跳过")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

AUX_PROVIDER_ID = "aux-provider-8f2"
AUX_MODEL = "aux-model-8f2"
AUX_MODEL_B = "aux-model-8f2-b"
AUX_API_KEY = "sk-SECRET-LEAK-CANARY-8f2"
AUX_TEXT_MARKER = "AUX_TEXT_MARKER"
DEEPSEEK_TEXT_MARKER = "DEEPSEEK_TEXT_MARKER"
NOW = "2026-10-21T14:00:00"

OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_PATH = "/v1/messages"

AUX_BODY = json.dumps({"prompt": "AUX_IMG_MARKER", "text": AUX_TEXT_MARKER},
                      ensure_ascii=False)

_DRIVER = r'''
import datetime, json, sys

spec = json.loads(sys.stdin.read())

import db
db.init()

from generators.situation import Activity, SituationContext
from generators.wildcard import WildcardPick
from generators.world import WorldSnapshot

now = datetime.datetime.fromisoformat(spec["now"])
situation = SituationContext(
    recurring=Activity(name="自由时间", description="在家放松", state="free"),
    sporadic=None, hobby=None)
wildcard = WildcardPick(card_id=1, card=spec["card"], emotion="开心")

import moments.post as post
moment_id = post.maybe_post_moment(
    spec["bot_id"], spec["bot_cfg"], now, situation, WorldSnapshot(),
    wildcard, 0.9, ["开心"], 120, spec["global_cfg"], None)

out = {"moment_id": moment_id}
if moment_id is not None:
    row = db.get_moment(moment_id)
    out["text"] = row["text"] if row else None
print("RESULT_JSON=" + json.dumps(out, ensure_ascii=False))
'''


class FakeAux:
    """本机回环假上游，只记录请求方法/路径/请求体，不记录请求头（避免 api_key 进测试输出）。"""

    def __init__(self):
        self.requests = []
        self._srv = None
        self._thread = None

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                stub.requests.append({"method": self.command, "path": self.path, "body": body})
                raw = json.dumps({
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": AUX_BODY}}],
                    "content": [{"type": "text", "text": AUX_BODY}],
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_POST = _handle
            do_GET = _handle

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    @property
    def root(self):
        """假上游根地址（无路径）。"""
        return "http://127.0.0.1:%d" % self._srv.server_address[1]

    @property
    def base(self):
        """带 /v1 的假上游地址。"""
        return self.root + "/v1"


@pytest.fixture
def aux():
    server = FakeAux().start()
    try:
        yield server
    finally:
        server.stop()


class World:
    """一次用例的隔离世界：HOME / 配置 / 库 / 假 CLI 全在 tmp。"""

    def __init__(self, base):
        self.base = Path(base)
        self.home = self.base / "home"
        self.legacy = self.base / "legacy"
        self.configs = self.base / "configs"
        self.channel = self.base / "channel"
        for d in (self.home / ".dsh-bot", self.home / "claudebotlife" / "configs",
                  self.legacy / "configs", self.configs, self.channel):
            d.mkdir(parents=True, exist_ok=True)
        self.providers_path = self.configs / "providers.json"
        self.global_yml = self.base / "_global.yml"
        self.state_db = self.base / "state.db"
        self.fake_log = self.base / "fake-claude.log"
        self.fake_claude = self.base / "fake-claude"
        self.fake_claude.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "' + str(self.fake_log) + '"\n'
            "printf '%s' '{\"signature\": \"DEEPSEEK_SIG_MARKER\", "
            "\"text\": \"" + DEEPSEEK_TEXT_MARKER + "\"}'\n",
            encoding="utf-8")
        self.fake_claude.chmod(0o755)
        self.set_providers([])
        self.set_global_yml(aux_llm=None)

    def set_providers(self, providers):
        raw = json.dumps({"providers": list(providers), "bindings": {}}, ensure_ascii=False)
        self.providers_path.write_text(raw, encoding="utf-8")
        (self.home / "claudebotlife" / "configs" / "providers.json").write_text(
            raw, encoding="utf-8")

    def set_global_yml(self, aux_llm=None):
        lines = ["moments:", "  enabled: true",
                 "  image_generation:", "    enabled: false"]
        if aux_llm:
            lines += ["aux_llm:", "  provider_id: %s" % aux_llm["provider_id"],
                      "  model: %s" % aux_llm["model"]]
        self.global_yml.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def provider(self, base_url, *, models, protocols=None, protocol=None):
        prov = {"id": AUX_PROVIDER_ID, "name": "假 aux 供应商",
                "base_url": base_url, "api_key": AUX_API_KEY,
                "models": [{"id": m, "label": m} for m in models]}
        if protocols is not None:
            prov["protocols"] = dict(protocols)
        if protocol is not None:
            prov["protocol"] = protocol
        return prov

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if k.split("_")[0].upper() not in ("CLAUDE", "DSH", "HUB", "BOTLIFE")
               and "proxy" not in k.lower()}
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "HUB_CONFIGS_DIR": str(self.configs),
            "DSH_BOT_HOME": str(self.home / ".dsh-bot"),
            # 三个接缝变量成组设置（同一临时目录）
            "CLAUDEBOT_CONFIG_DIR": str(self.configs),
            "CLAUDEBOT_CHANNELS_ROOT": str(self.channel),
            "CLAUDEBOT_PROVIDER_PATH": str(self.providers_path),
            "CLAUDEBOT_LEGACY_ROOT": str(self.legacy),
            "CLAUDEBOT_GLOBAL_CFG": str(self.global_yml),
            "BOTLIFE_STATE_DB": str(self.state_db),
            "CLAUDE_BIN": str(self.fake_claude),
            "CLAUDEBOTLIFE_FORCE_CLAUDE": "1",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        })
        return env

    def run(self, timeout=90):
        global_cfg = {"user_display_name": "我", "user_bio": "签名",
                      "moments": {"enabled": True, "daily_post_limit_per_bot": 5,
                                  "image_generation": {"enabled": False}}}
        bot_cfg = {"id": "tbot", "display_name": "测试", "chat_id": "42",
                   "bot_channel_path": str(self.channel),
                   "moments": {"enabled": True, "daily_post_limit_per_bot": 5,
                               "image_generation": {"enabled": False}},
                   "moments_daily_limit": 5}
        spec = {"bot_id": "tbot", "bot_cfg": bot_cfg, "global_cfg": global_cfg,
                "now": NOW, "card": "今天心情很好"}
        proc = subprocess.run(
            [sys.executable, "-c", _DRIVER], cwd=str(ROOT), env=self.env(),
            input=json.dumps(spec, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return Run(proc)

    def claude_calls(self):
        try:
            return [ln for ln in self.fake_log.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        except FileNotFoundError:
            return []


class Run:
    def __init__(self, proc):
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    def result(self):
        lines = [ln for ln in self.out.splitlines() if ln.startswith("RESULT_JSON=")]
        assert lines, "子进程没有输出结果；退出码 %s，stderr：%s" % (self.rc, self.err[-800:])
        return json.loads(lines[-1][len("RESULT_JSON="):])


def _assert_moment_posted(run):
    assert run.rc == 0, "朋友圈路径把异常抛给了调用方；stderr：%s" % run.err[-800:]
    result = run.result()
    assert isinstance(result.get("moment_id"), int), \
        "没有拿到朋友圈 id，朋友圈未生成：%r" % result
    return result


def _assert_aux_text(result):
    text = result.get("text") or ""
    assert AUX_TEXT_MARKER in text, "朋友圈正文不是 aux 供应商返回的内容：%r" % text
    assert DEEPSEEK_TEXT_MARKER not in text, "正文混进了原 DeepSeek 路径的输出：%r" % text


def _paths(aux):
    return [r["path"] for r in aux.requests]


def _assert_one_request_ends_with(aux, suffix):
    paths = _paths(aux)
    assert len(paths) == 1, "aux 上游应收到 1 次请求，实际 %r" % paths
    assert paths[0].endswith(suffix), \
        "上游请求路径应以 %s 结尾，实际 %r" % (suffix, paths[0])
    return paths[0]


# ── 1. 供应商写了 protocol: openai：请求 /v1/chat/completions ─────────────

def test_供应商标openai_请求chat_completions端点(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base, models=[AUX_MODEL], protocol="openai")])
    w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})
    run = w.run()

    result = _assert_moment_posted(run)
    _assert_aux_text(result)
    assert _paths(aux) == [OPENAI_PATH], \
        "供应商标 openai，上游应收到 %s，实际 %r" % (OPENAI_PATH, _paths(aux))
    assert w.claude_calls() == [], "供应商协议可用时不该回退 DeepSeek"


# ── 2. 供应商写了 protocol: anthropic：请求 /v1/messages ──────────────────

def test_供应商标anthropic_请求messages端点(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base, models=[AUX_MODEL], protocol="anthropic")])
    w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})
    run = w.run()

    result = _assert_moment_posted(run)
    _assert_aux_text(result)
    assert _paths(aux) == [ANTHROPIC_PATH], \
        "供应商标 anthropic，上游应收到 %s，实际 %r" % (ANTHROPIC_PATH, _paths(aux))
    assert w.claude_calls() == [], "供应商协议可用时不该回退 DeepSeek"


# ── 3. 没写协议、base_url 路径含 anthropic 段：请求 /v1/messages ──────────

def test_未标协议且地址含anthropic段_请求messages端点(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.root + "/anthropic", models=[AUX_MODEL])])
    w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})
    run = w.run()

    result = _assert_moment_posted(run)
    _assert_aux_text(result)
    _assert_one_request_ends_with(aux, ANTHROPIC_PATH)
    assert w.claude_calls() == [], "由地址推断出 anthropic 时不该回退 DeepSeek"


# ── 4. 没写协议、base_url 路径含 v1 段：请求 /v1/chat/completions ─────────

def test_未标协议且地址含v1段_请求chat_completions端点(aux, tmp_path):
    w = World(tmp_path)
    # 路径只有 /v1，没有 anthropic 段
    w.set_providers([w.provider(aux.base, models=[AUX_MODEL])])
    w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})
    run = w.run()

    result = _assert_moment_posted(run)
    _assert_aux_text(result)
    assert _paths(aux) == [OPENAI_PATH], \
        "地址含 v1 段应按 OpenAI 请求 %s，实际 %r" % (OPENAI_PATH, _paths(aux))
    assert w.claude_calls() == [], "由地址推断出 openai 时不该回退 DeepSeek"


# ── 5. 没写协议、地址也没有线索：默认 OpenAI 请求 ────────────────────────

def test_未标协议且地址无线索_默认请求chat_completions端点(aux, tmp_path):
    w = World(tmp_path)
    # 地址无路径，既无 anthropic 也无 v1 段
    w.set_providers([w.provider(aux.root, models=[AUX_MODEL])])
    w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})
    run = w.run()

    result = _assert_moment_posted(run)
    _assert_aux_text(result)
    _assert_one_request_ends_with(aux, OPENAI_PATH)
    assert w.claude_calls() == [], "默认 OpenAI 时不该回退 DeepSeek"


# ── 6. 同一供应商下两个模型都按供应商协议走（逐模型标注不参与）────────────

def test_同一供应商下两个模型_都按供应商协议请求(aux, tmp_path):
    w_a = World(tmp_path / "model-a")
    w_b = World(tmp_path / "model-b")
    for w, model in ((w_a, AUX_MODEL), (w_b, AUX_MODEL_B)):
        # 供应商整体标 anthropic；模型列表含两个模型，均无逐模型标注
        w.set_providers([w.provider(aux.base,
                                    models=[AUX_MODEL, AUX_MODEL_B],
                                    protocol="anthropic")])
        w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": model})

    result_a = _assert_moment_posted(w_a.run())
    result_b = _assert_moment_posted(w_b.run())
    _assert_aux_text(result_a)
    _assert_aux_text(result_b)

    assert _paths(aux) == [ANTHROPIC_PATH, ANTHROPIC_PATH], \
        "两个不同模型都应跟随供应商协议请求 %s，实际 %r" % (ANTHROPIC_PATH, _paths(aux))
    # 模型只是请求体里的字段，不影响协议判定
    assert json.loads(aux.requests[0]["body"]).get("model") == AUX_MODEL
    assert json.loads(aux.requests[1]["body"]).get("model") == AUX_MODEL_B
    assert w_a.claude_calls() == [] and w_b.claude_calls() == [], \
        "供应商协议可用时都不该回退 DeepSeek"


# ── 7. 回归：供应商条目残留相反的逐模型标注，仍以供应商为准 ──────────────

def test_供应商协议优先于残留的逐模型标注(aux, tmp_path):
    w = World(tmp_path)
    # 供应商标 openai；base_url 路径含 anthropic；逐模型残留标注 anthropic。
    # 按新口径：供应商 protocol 最优先，应请求 OpenAI 端点。
    w.set_providers([w.provider(aux.root + "/anthropic",
                                models=[AUX_MODEL],
                                protocol="openai",
                                protocols={AUX_MODEL: "anthropic"})])
    w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})
    run = w.run()

    result = _assert_moment_posted(run)
    _assert_aux_text(result)
    assert _paths(aux) == [OPENAI_PATH], \
        "供应商 protocol 应压过逐模型标注与地址线索，预期 %s，实际 %r" % (
            OPENAI_PATH, _paths(aux))
    assert w.claude_calls() == [], "供应商协议可用时不该回退 DeepSeek"
