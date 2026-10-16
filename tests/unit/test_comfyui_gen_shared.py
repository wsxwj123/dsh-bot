# -*- coding: utf-8 -*-
"""两边共用的 ComfyUI 生图脚本（skills/comfyui-skill/scripts/comfyui_gen.py，约定 v1.1 5.21 第 4 条）。

装好后在 ~/.claude/skills/comfyui-skill/ 下，两种底层都调这一份，所以脚本不能 import 任何仓库里的模块：
全局配置与这只 bot 的锚点都按参数、环境变量去找。整条链路用本机线程里的假 ComfyUI 跑，
不碰真实的 ComfyUI，输出都写在 tmp_path 下。
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "comfyui-skill" / "scripts" / "comfyui_gen.py"

WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "denoise": 1.0}},
    "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1, "height": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "%prompt%"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "%negative_prompt%"}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
}


class FakeComfy:
    """POST /prompt 记下收到的工作流；GET /history/<id> 按 mode 回"完成"或"执行报错"。"""

    def __init__(self, out_dir: Path, mode: str = "ok"):
        self.out_dir, self.mode, self.posted = out_dir, mode, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.posted.append(json.loads(self.rfile.read(n)))
                self._send({"prompt_id": "p1"})

            def do_GET(self):
                if fake.mode == "error":
                    return self._send({"p1": {"status": {"status_str": "error", "completed": False,
                                                         "messages": [["execution_error", {"exception_message": "boom"}]]}}})
                (fake.out_dir / "a.png").write_bytes(b"png")
                self._send({"p1": {"status": {"status_str": "success", "completed": True},
                                   "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": ""}]}}}})

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _setup(tmp_path: Path, url: str, **extra) -> Path:
    """在 tmp_path 下摆一份 ComfyUI 目录与全局配置，返回全局配置路径。"""
    out_dir = tmp_path / "ComfyUI" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps(WORKFLOW), encoding="utf-8")
    cfy = {"url": url, "workflow": str(wf), "output_dir": str(out_dir), **extra}
    g = {"moments": {"image_generation": {"comfyui": cfy, "positive_prefix": "best quality", "negative_prefix": "lowres"}}}
    gpath = tmp_path / "_global.yml"
    gpath.write_text(yaml.safe_dump(g, allow_unicode=True), encoding="utf-8")
    return gpath


def _run(args, env_extra=None, timeout=60):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDEBOT_GLOBAL_CFG", "CLAUDEBOTLIFE_REPO")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=timeout)


def _last(r) -> str:
    lines = r.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def _mod():
    spec = importlib.util.spec_from_file_location("comfyui_gen_shared", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def fake(tmp_path):
    f = FakeComfy(tmp_path / "ComfyUI" / "output")
    yield f
    f.close()


def test_script_has_no_repo_imports_or_personal_paths():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "config_loader" not in src
    assert "/Users/" not in src


def test_success_prints_media_and_builds_prompt_from_both_configs(tmp_path, fake):
    gpath = _setup(tmp_path, fake.url)
    bot = tmp_path / "bot.yml"
    bot.write_text("face_traits: long black hair\n", encoding="utf-8")
    out = tmp_path / "res.png"
    r = _run(["yasuna", "1girl, selfie, smiling", "--global", str(gpath), "--bot-config", str(bot), "--out", str(out)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last(r) == f"MEDIA: {out}"
    assert out.read_bytes() == b"png"
    wf = fake.posted[0]["prompt"]
    assert wf["6"]["inputs"]["text"] == "best quality, long black hair, 1girl, selfie, smiling"
    assert wf["7"]["inputs"]["text"] == "lowres"


def test_global_config_is_found_by_env_then_repo(tmp_path, fake):
    gpath = _setup(tmp_path, fake.url)
    r = _run(["yasuna", "a cup of tea", "--out", str(tmp_path / "1.png")], {"CLAUDEBOT_GLOBAL_CFG": str(gpath)})
    assert r.returncode == 0, r.stdout + r.stderr
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs" / "_global.yml").write_text(gpath.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "configs" / "yasuna.yml").write_text("face_traits: short hair\n", encoding="utf-8")
    r = _run(["yasuna", "1girl, portrait", "--out", str(tmp_path / "2.png")], {"CLAUDEBOTLIFE_REPO": str(repo)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert fake.posted[-1]["prompt"]["6"]["inputs"]["text"] == "best quality, short hair, 1girl, portrait"


def test_lookup_order(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.delenv("CLAUDEBOT_GLOBAL_CFG", raising=False)
    monkeypatch.delenv("CLAUDEBOTLIFE_REPO", raising=False)
    assert m.resolve_global_cfg(None) is None
    assert m.resolve_bot_cfg(None, "yasuna") is None
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    monkeypatch.setenv("CLAUDEBOTLIFE_REPO", str(repo))
    assert m.resolve_global_cfg(None) == str(repo / "configs" / "_global.yml")
    assert m.resolve_bot_cfg(None, "yasuna") is None            # 仓库里没有这只的 yml：不用锚点
    (repo / "configs" / "yasuna.yml").write_text("face_traits: x\n", encoding="utf-8")
    assert m.resolve_bot_cfg(None, "yasuna") == str(repo / "configs" / "yasuna.yml")
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(tmp_path / "env.yml"))
    assert m.resolve_global_cfg(None) == str(tmp_path / "env.yml")
    assert m.resolve_global_cfg(str(tmp_path / "arg.yml")) == str(tmp_path / "arg.yml")
    assert m.resolve_bot_cfg(str(tmp_path / "b.yml"), "yasuna") == str(tmp_path / "b.yml")


def test_no_global_config_fails_with_one_line(tmp_path):
    r = _run(["yasuna", "1girl", "--out", str(tmp_path / "o.png")])
    assert r.returncode == 1
    assert _last(r).startswith("FAIL: ")
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", ""])
def test_bad_bot_id_is_rejected(tmp_path, fake, bad):
    gpath = _setup(tmp_path, fake.url)
    r = _run([bad, "1girl", "--global", str(gpath), "--out", str(tmp_path / "o.png")])
    assert r.returncode == 1
    assert _last(r).startswith("FAIL: ")
    assert fake.posted == []


def test_unreachable_comfyui_fails_with_reason(tmp_path):
    gpath = _setup(tmp_path, f"http://127.0.0.1:{_free_port()}")
    r = _run(["yasuna", "1girl", "--global", str(gpath), "--out", str(tmp_path / "o.png")], timeout=30)
    assert r.returncode == 1
    assert _last(r).startswith("FAIL: 连不上 ComfyUI")
    assert "Traceback" not in r.stderr


def test_execution_error_returns_at_once(tmp_path):
    f = FakeComfy(tmp_path / "ComfyUI" / "output", mode="error")
    try:
        gpath = _setup(tmp_path, f.url)
        t0 = time.monotonic()
        r = _run(["yasuna", "1girl", "--global", str(gpath), "--out", str(tmp_path / "o.png")])
        assert r.returncode == 1
        assert _last(r).startswith("FAIL: ComfyUI 执行报错")
        assert time.monotonic() - t0 < 20                          # 不再白等到 600 秒超时
    finally:
        f.close()


def test_anchor_goes_img2img_and_staged_copy_is_removed(tmp_path, fake):
    from PIL import Image
    anchor = tmp_path / "anchor.png"
    Image.new("RGB", (64, 96), "white").save(anchor)
    wf2 = tmp_path / "wf2.json"
    wf2.write_text(json.dumps(WORKFLOW), encoding="utf-8")
    gpath = _setup(tmp_path, fake.url, workflow_img2img=str(wf2))
    bot = tmp_path / "bot.yml"
    bot.write_text(yaml.safe_dump({"anchor_image": str(anchor), "anchor_denoise": 0.8}), encoding="utf-8")
    r = _run(["yasuna", "1girl, selfie", "--global", str(gpath), "--bot-config", str(bot), "--out", str(tmp_path / "o.png")])
    assert r.returncode == 0, r.stdout + r.stderr
    wf = fake.posted[0]["prompt"]
    staged = wf["10"]["inputs"]["image"]
    assert staged.startswith("_init_")
    assert wf["3"]["inputs"]["denoise"] == 0.8
    assert not (tmp_path / "ComfyUI" / "input" / staged).exists()   # 放进 input 的起点图用完即删


def test_old_location_forwards_to_the_shared_script(tmp_path):
    """仓库里旧位置 scripts/comfyui_gen.py 只做转发：老的技能说明还在调这里时，跑的也是共用的那一份。"""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "comfyui_gen.py"), "../x", "1girl"],
                       capture_output=True, text=True, timeout=60,
                       env={k: v for k, v in os.environ.items() if k not in ("CLAUDEBOT_GLOBAL_CFG", "CLAUDEBOTLIFE_REPO")})
    assert r.returncode == 1
    assert _last(r).startswith("FAIL: bot 编号不合规")
    assert len((ROOT / "scripts" / "comfyui_gen.py").read_text(encoding="utf-8").splitlines()) < 20
