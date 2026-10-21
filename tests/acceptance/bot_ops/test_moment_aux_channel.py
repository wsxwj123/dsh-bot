# -*- coding: utf-8 -*-
"""moment-aux-channel 验收测试（黑盒）。

依据：`.devflow/moment-aux-channel-接口.md` 的行为清单与错误契约，一条行为一个用例。

怎么做到"只走对外接口"：
  * 每个用例起一个**干净的子进程**，用仓库根的应用入口调用公开函数
    `moments.post.maybe_post_moment`（接口点名的观测点）与 `signature.maybe_update_signature`；
    测试进程自己不 import 产品模块、不 mock 任何内部层。
  * 上游用本机回环假 HTTP 服务模拟：正常正文 / HTTP 500 / 回执无正文。
  * 原 DeepSeek 路径用 `CLAUDE_BIN` 指向假 CLI（记录调用、输出可识别文案），
    并强制 CLI 模式，绝不发真实外网请求。
  * HOME、HUB_CONFIGS_DIR、CLAUDEBOT_PROVIDER_PATH、CLAUDEBOT_LEGACY_ROOT、
    BOTLIFE_STATE_DB、CLAUDEBOT_GLOBAL_CFG 全部钉在 pytest 临时目录。

断言的口径：朋友圈落库的正文（`db.get_moment` 的 text，即对外可见的朋友圈内容）、
子进程退出码、假上游收到的请求数、假 CLI 调用次数、子进程输出的诊断日志。
"""
import json
import os
import socket
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
AUX_API_KEY = "sk-SECRET-LEAK-CANARY-8f2"
AUX_TEXT_MARKER = "AUX_TEXT_MARKER"
AUX_IMG_MARKER = "AUX_IMG_MARKER"
DEEPSEEK_TEXT_MARKER = "DEEPSEEK_TEXT_MARKER"
NOW = "2026-10-21T14:00:00"

# 假上游对所有请求都回一段同时满足"文案"和"JSON 翻译"的正文，
# 这样不依赖实现先调文案还是先调翻译。
AUX_BODY = json.dumps({"prompt": AUX_IMG_MARKER, "text": AUX_TEXT_MARKER}, ensure_ascii=False)

# 子进程驱动：只调公开入口，把结果打成一行 RESULT_JSON=... 交给测试。
_DRIVER = r'''
import datetime, json, os, sys, time

spec = json.loads(sys.stdin.read())
scenario = spec["scenario"]

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
out = {"scenario": scenario}

if scenario in ("moment", "translation"):
    import moments.post as post
    moment_id = post.maybe_post_moment(
        spec["bot_id"], spec["bot_cfg"], now, situation, WorldSnapshot(),
        wildcard, 0.9, ["开心"], 120, spec["global_cfg"], None)
    if scenario == "translation":
        # 生图翻译可能是异步触发，给它一点时间把上游请求发出去
        time.sleep(2.0)
    out["moment_id"] = moment_id
    if moment_id is not None:
        row = db.get_moment(moment_id)
        out["text"] = row["text"] if row else None
elif scenario == "signature":
    import signature
    out["signature"] = signature.maybe_update_signature(
        spec["bot_id"], spec["bot_cfg"], 0.5, ["平静"], None, None,
        WorldSnapshot(), force=True)
else:
    raise SystemExit("未知 scenario: %s" % scenario)

print("RESULT_JSON=" + json.dumps(out, ensure_ascii=False))
'''


class FakeAux:
    """本机回环假上游。mode: ok | error | empty。只记路径与正文，不记请求头（避免 api_key 进测试输出）。"""

    def __init__(self):
        self.requests = []
        self.mode = "ok"
        self.body = None  # None 表示用默认 AUX_BODY；用例可替换
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
                reply_text = stub.body if stub.body is not None else AUX_BODY
                if stub.mode == "error":
                    raw = json.dumps({"error": "upstream boom"}).encode("utf-8")
                    self.send_response(500)
                elif stub.mode == "empty":
                    raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": ""}}],
                                      "content": []}).encode("utf-8")
                    self.send_response(200)
                else:
                    raw = json.dumps({
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": reply_text}}],
                        "content": [{"type": "text", "text": reply_text}],
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
    def base(self):
        return "http://127.0.0.1:%d/v1" % self._srv.server_address[1]

    @property
    def base_no_v1(self):
        """同一假上游，但 base_url 不带 /v1 的写法。"""
        return "http://127.0.0.1:%d" % self._srv.server_address[1]


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
        # 假 comfyui 技能目录：只放最小结构，让生图翻译步骤能走到；出图本身不在本次范围。
        self.comfy_skill = self.base / "comfyui-skill"
        (self.comfy_skill / "scripts").mkdir(parents=True, exist_ok=True)
        (self.comfy_skill / "SKILL.md").write_text(
            "---\nname: comfyui-skill\n---\n# 假技能\n", encoding="utf-8")
        fake_comfy = self.comfy_skill / "scripts" / "comfyui_gen.py"
        fake_comfy.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "p = pathlib.Path(sys.argv[0]).with_name('fake-output.png')\n"
            "p.write_bytes(b'\\x89PNG\\r\\n\\x1a\\n')\n"
            "print('MEDIA: %s' % p)\n",
            encoding="utf-8")
        fake_comfy.chmod(0o755)
        self.fake_claude.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "' + str(self.fake_log) + '"\n'
            "printf '%s' '{\"signature\": \"DEEPSEEK_SIG_MARKER\", "
            "\"text\": \"" + DEEPSEEK_TEXT_MARKER + "\"}'\n",
            encoding="utf-8")
        self.fake_claude.chmod(0o755)
        self.set_providers([])
        self.set_global_yml(aux_llm=None, image_enabled=False)

    # -- 预置 ---------------------------------------------------------------
    def set_providers(self, providers):
        data = {"providers": list(providers), "bindings": {}}
        raw = json.dumps(data, ensure_ascii=False)
        self.providers_path.write_text(raw, encoding="utf-8")
        # 实现可能按 HOME 拼 ~/claudebotlife/configs/providers.json，两处写同一份
        (self.home / "claudebotlife" / "configs" / "providers.json").write_text(
            raw, encoding="utf-8")

    def set_global_yml(self, aux_llm=None, image_enabled=False,
                       image_provider_moment=None):
        lines = ["moments:", "  enabled: true"]
        if image_enabled:
            engine = image_provider_moment or "comfyui"
            lines += ["  image_generation:", "    enabled: true",
                      "    provider: %s" % engine,
                      "    provider_moment: %s" % engine,
                      "    image_when:", "    - selfie_keyword",
                      "    - mood_extreme", "    - sporadic_positive",
                      "    - special_date", "    - hobby_visual",
                      "    - private_intimate",
                      "    comfyui:",
                      "      url: http://127.0.0.1:%d" % _closed_port(),
                      "      workflow: /nonexistent-workflow.json",
                      "      workflow_img2img: /nonexistent-workflow.json",
                      "      output_dir: %s" % (self.base / "img-out"),
                      "      prompt_placeholder: '%prompt%'",
                      "      negative_placeholder: '%negative_prompt%'",
                      "image:", "  enabled: true",
                      "  provider_moment: %s" % engine]
        else:
            lines += ["  image_generation:", "    enabled: false"]
        if aux_llm:
            lines += ["aux_llm:", "  provider_id: %s" % aux_llm["provider_id"],
                      "  model: %s" % aux_llm["model"]]
        self.global_yml.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def provider(self, base_url, models=None):
        return {"id": AUX_PROVIDER_ID, "name": "假 aux 供应商",
                "base_url": base_url, "api_key": AUX_API_KEY,
                "models": list(models or [AUX_MODEL]), "protocol": "openai"}

    # -- 运行 ---------------------------------------------------------------
    def env(self):
        env = {k: v for k, v in os.environ.items()
               if k.split("_")[0].upper() not in ("CLAUDE", "DSH", "HUB", "BOTLIFE")
               and "proxy" not in k.lower()}
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "HUB_CONFIGS_DIR": str(self.configs),
            "DSH_BOT_HOME": str(self.home / ".dsh-bot"),
            "CLAUDEBOT_PROVIDER_PATH": str(self.providers_path),
            "CLAUDEBOT_LEGACY_ROOT": str(self.legacy),
            "CLAUDEBOT_GLOBAL_CFG": str(self.global_yml),
            "BOTLIFE_STATE_DB": str(self.state_db),
            "CLAUDE_BIN": str(self.fake_claude),
            "COMFYUI_SKILL_DIR": str(self.comfy_skill),
            "CLAUDEBOTLIFE_FORCE_CLAUDE": "1",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        })
        return env

    def run(self, scenario, *, aux_llm=None, card="今天心情很好",
            image_enabled=False, timeout=90):
        global_cfg = {"user_display_name": "我", "user_bio": "签名",
                      "moments": {"enabled": True, "daily_post_limit_per_bot": 5,
                                  "image_generation": {"enabled": False}}}
        bot_cfg = {"id": "tbot", "display_name": "测试", "chat_id": "42",
                   "bot_channel_path": str(self.channel),
                   "moments": {"enabled": True, "daily_post_limit_per_bot": 5,
                               "image_generation": {"enabled": False}},
                   "moments_daily_limit": 5}
        if aux_llm:
            global_cfg["aux_llm"] = dict(aux_llm)
        if image_enabled:
            img = {"enabled": True, "provider": "comfyui", "provider_moment": "comfyui",
                   "image_when": ["selfie_keyword"],
                   "comfyui": {"url": "http://127.0.0.1:%d" % _closed_port(),
                               "workflow": "/nonexistent-workflow.json",
                               "workflow_img2img": "/nonexistent-workflow.json",
                               "output_dir": str(self.base / "img-out"),
                               "prompt_placeholder": "%prompt%",
                               "negative_placeholder": "%negative_prompt%"}}
            global_cfg["moments"]["image_generation"] = img
            bot_cfg["moments"]["image_generation"] = img
        spec = {"scenario": scenario, "bot_id": "tbot", "bot_cfg": bot_cfg,
                "global_cfg": global_cfg, "now": NOW, "card": card}
        proc = subprocess.run(
            [sys.executable, "-c", _DRIVER], cwd=str(ROOT), env=self.env(),
            input=json.dumps(spec, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return Run(proc)

    # -- 观测 ---------------------------------------------------------------
    def claude_calls(self):
        try:
            return [ln for ln in self.fake_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except FileNotFoundError:
            return []

    def reset_claude_log(self):
        try:
            self.fake_log.unlink()
        except FileNotFoundError:
            pass


class Run:
    def __init__(self, proc):
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    @property
    def output(self):
        return self.out + "\n" + self.err

    def result(self):
        lines = [ln for ln in self.out.splitlines() if ln.startswith("RESULT_JSON=")]
        assert lines, "子进程没有输出结果；退出码 %s，stderr：%s" % (self.rc, self.err[-800:])
        return json.loads(lines[-1][len("RESULT_JSON="):])


def _assert_moment_posted(run):
    assert run.rc == 0, "朋友圈路径把异常抛给了调用方；stderr：%s" % run.err[-800:]
    result = run.result()
    assert isinstance(result.get("moment_id"), int), "没有拿到朋友圈 id，朋友圈未生成：%r" % result
    return result


# ── 1. aux_llm 有效：文案由该供应商生成，不调 DeepSeek ───────────────────

def test_aux配置有效_朋友圈文案由aux供应商生成且不调DeepSeek(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base)])
    run = w.run("moment", aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})

    result = _assert_moment_posted(run)
    text = result.get("text") or ""
    assert AUX_TEXT_MARKER in text or AUX_IMG_MARKER in text, \
        "朋友圈正文不是 aux 供应商返回的内容：%r" % text
    assert DEEPSEEK_TEXT_MARKER not in text, "文案里混进了原 DeepSeek 路径的输出：%r" % text
    assert len(aux.requests) == 1, "aux 上游应收到 1 次文案生成请求，实际 %d 次" % len(aux.requests)
    assert w.claude_calls() == [], "aux 通道生效时不该再调用原 DeepSeek 路径"


# ── 2. aux_llm 没配：照旧走 DeepSeek ─────────────────────────────────────

def test_未配置aux_llm_朋友圈文案仍走DeepSeek(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base)])
    run = w.run("moment", aux_llm=None)

    result = _assert_moment_posted(run)
    assert DEEPSEEK_TEXT_MARKER in (result.get("text") or ""), \
        "没配 aux_llm 时文案没有走原 DeepSeek 路径：%r" % result.get("text")
    assert aux.requests == [], "没配 aux_llm 却请求了 aux 上游"
    assert len(w.claude_calls()) == 1, "原 DeepSeek 路径应被调用 1 次，实际 %d 次" % len(w.claude_calls())


# ── 3. provider_id 指向不存在的供应商：回退 ──────────────────────────────

def test_aux供应商不存在_回退DeepSeek且朋友圈照常生成(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base)])
    run = w.run("moment", aux_llm={"provider_id": "no-such-provider", "model": AUX_MODEL})

    result = _assert_moment_posted(run)
    assert DEEPSEEK_TEXT_MARKER in (result.get("text") or ""), "供应商不存在时没有回退 DeepSeek"
    assert aux.requests == [], "供应商不存在，不该访问任何 aux 上游"
    assert len(w.claude_calls()) == 1, "回退时应调用原 DeepSeek 路径 1 次"


# ── 4. 模型不在该供应商模型列表里：回退 ─────────────────────────────────

def test_aux模型不在供应商列表_回退DeepSeek(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base, models=["some-other-model"])])
    run = w.run("moment", aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})

    result = _assert_moment_posted(run)
    assert DEEPSEEK_TEXT_MARKER in (result.get("text") or ""), "模型不在列表里时没有回退 DeepSeek"
    assert aux.requests == [], "模型不在列表里，不该访问 aux 上游"
    assert len(w.claude_calls()) == 1, "回退时应调用原 DeepSeek 路径 1 次"


# ── 5. 上游错误状态或连接失败：回退，不中断 ─────────────────────────────

def test_aux上游错误或连接失败_回退DeepSeek且朋友圈不中断(aux, tmp_path):
    aux_llm = {"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL}

    # 5a. HTTP 500（独立世界：同库第二次发会被「同 kind 4h 不重发」挡住）
    aux.mode = "error"
    w = World(tmp_path / "http-error")
    w.set_providers([w.provider(aux.base)])
    run = w.run("moment", aux_llm=aux_llm)
    result = _assert_moment_posted(run)
    assert DEEPSEEK_TEXT_MARKER in (result.get("text") or ""), "上游 500 时没有回退 DeepSeek"
    assert len(aux.requests) == 1, "应尝试过 aux 上游一次"
    assert len(w.claude_calls()) == 1, "上游 500 后应调用原 DeepSeek 路径 1 次"

    # 5b. 连接失败（指向没人监听的端口）
    aux.mode = "ok"
    before = len(aux.requests)
    w = World(tmp_path / "conn-refused")
    w.set_providers([w.provider("http://127.0.0.1:%d/v1" % _closed_port())])
    run = w.run("moment", aux_llm=aux_llm)
    result = _assert_moment_posted(run)
    assert DEEPSEEK_TEXT_MARKER in (result.get("text") or ""), "连接失败时没有回退 DeepSeek"
    assert len(w.claude_calls()) == 1, "连接失败后应调用原 DeepSeek 路径 1 次"
    assert len(aux.requests) == before, "连接失败场景不该打到本机假上游"


# ── 6. 上游回执没有正文：回退 ────────────────────────────────────────────

def test_aux回执无正文_回退DeepSeek(aux, tmp_path):
    w = World(tmp_path)
    aux.mode = "empty"
    w.set_providers([w.provider(aux.base)])
    run = w.run("moment", aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})

    result = _assert_moment_posted(run)
    assert DEEPSEEK_TEXT_MARKER in (result.get("text") or ""), "回执无正文时没有回退 DeepSeek"
    assert len(aux.requests) == 1, "应尝试过 aux 上游一次"
    assert len(w.claude_calls()) == 1, "回执无正文后应调用原 DeepSeek 路径 1 次"


# ── 7. 生图提示词翻译也走 aux 通道 ──────────────────────────────────────

def test_生图提示词翻译走aux通道(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base)])
    # 朋友圈配图只在命中 image_when（selfie_keyword）时触发；让文案带自拍关键词，
    # 从而真的走到 comfyui 分支的生图提示词翻译。
    aux.body = json.dumps({"prompt": AUX_IMG_MARKER,
                           "text": AUX_TEXT_MARKER + " 自拍"},
                          ensure_ascii=False)
    w.set_global_yml(aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL},
                     image_enabled=True, image_provider_moment="comfyui")
    run = w.run("translation", aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL},
                card="今天想来自拍", image_enabled=True)

    result = _assert_moment_posted(run)
    translation_requests = [
        r for r in aux.requests
        if "ComfyUI" in r["body"] and "JSON" in r["body"]]
    assert translation_requests, \
        "aux 上游没有收到生图提示词翻译请求；实际收到 %d 次请求：%r" % (
            len(aux.requests), [r["body"][:200] for r in aux.requests])
    assert len(aux.requests) >= 2, \
        "文案生成与生图提示词翻译都应走 aux，aux 上游实际只收到 %d 次请求" % len(aux.requests)
    assert w.claude_calls() == [], "生图翻译走 aux 时不该再调用原 DeepSeek 路径"
    assert AUX_TEXT_MARKER in (result.get("text") or ""), \
        "朋友圈正文不是 aux 返回的内容"


# ── 8. 诊断日志只出现供应商 id 与模型名，不出现 api_key ──────────────────

def test_诊断日志含供应商id与模型名但不含api_key(aux, tmp_path):
    aux_llm = {"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL}
    # 两次运行各用独立世界/库：同库第二次发会被「同 kind 4h 不重发」挡住
    w_ok = World(tmp_path / "ok")
    w_ok.set_providers([w_ok.provider(aux.base)])
    ok_run = w_ok.run("moment", aux_llm=aux_llm)
    _assert_moment_posted(ok_run)

    aux.mode = "error"
    w_err = World(tmp_path / "err")
    w_err.set_providers([w_err.provider(aux.base)])
    err_run = w_err.run("moment", aux_llm=aux_llm)
    _assert_moment_posted(err_run)

    combined = ok_run.output + "\n" + err_run.output
    assert AUX_PROVIDER_ID in combined, "诊断日志里没有供应商 id：%r" % combined[-800:]
    assert AUX_MODEL in combined, "诊断日志里没有模型名：%r" % combined[-800:]
    assert AUX_API_KEY not in combined, "诊断日志里泄露了 api_key"
    assert AUX_API_KEY not in (ok_run.result().get("text") or ""), "朋友圈正文里出现了 api_key"


# ── 9. 个性签名不受影响，仍走原路径 ─────────────────────────────────────

def test_个性签名不受aux影响_仍走原路径(aux, tmp_path):
    w = World(tmp_path)
    w.set_providers([w.provider(aux.base)])
    run = w.run("signature", aux_llm={"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL})

    assert run.rc == 0, "签名路径把异常抛给了调用方；stderr：%s" % run.err[-800:]
    result = run.result()
    assert aux.requests == [], "个性签名不该走 aux 通道"
    assert len(w.claude_calls()) >= 1, "个性签名仍应走原 DeepSeek 路径"
    sig = result.get("signature")
    assert not (isinstance(sig, str) and "AUX_" in sig), "签名内容来自 aux 通道：%r" % sig


# ── 10. 请求地址规则：base_url 带不带 /v1 都发往 /v1/chat/completions ────

def test_openai协议请求路径_带不带v1都路由到v1_chat_completions(aux, tmp_path):
    aux_llm = {"provider_id": AUX_PROVIDER_ID, "model": AUX_MODEL}

    # 10a. base_url 不带 /v1，应补成 /v1/chat/completions
    w = World(tmp_path / "no-v1")
    w.set_providers([w.provider(aux.base_no_v1)])
    run = w.run("moment", aux_llm=aux_llm)
    _assert_moment_posted(run)
    paths = [r["path"] for r in aux.requests]
    assert paths == ["/v1/chat/completions"], \
        "base_url 不带 /v1 时，aux 请求路径应为 /v1/chat/completions，实际 %r" % paths

    # 10b. base_url 已带 /v1，不能重复拼成 /v1/v1/chat/completions
    #      （独立世界：同库第二次发会被「同 kind 4h 不重发」挡住）
    aux.requests.clear()
    w2 = World(tmp_path / "with-v1")
    w2.set_providers([w2.provider(aux.base)])
    run2 = w2.run("moment", aux_llm=aux_llm)
    _assert_moment_posted(run2)
    paths2 = [r["path"] for r in aux.requests]
    assert not any("/v1/v1/" in p for p in paths2), \
        "base_url 末尾的 /v1 被重复拼接：%r" % paths2
    assert paths2 == ["/v1/chat/completions"], \
        "base_url 已带 /v1 时，aux 请求路径应为 /v1/chat/completions，实际 %r" % paths2
