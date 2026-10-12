# -*- coding: utf-8 -*-
"""styles 接口验收的夹具：整套跑在 Flask 测试客户端上，不监听端口、不碰真实 styles.json。

为什么不像线上那样起服务再打 HTTP：本机随时可能有一个真的 moments 服务在跑，
测试一旦连上去就会改乱真实的 bot 画风绑定。测试客户端走同一套路由与 JSON 契约，
但没有 socket，也就没有打错端口的可能。
"""


def _require_total_gate():
    """自检：tests/conftest.py 的总闸没加载，就拒绝运行本目录的用例。

    pytest 只加载「启动目录及其下」的 conftest。cd 到本目录（或它的某个上级子目录）里面
    再起 pytest，总闸不会被加载：HUB_LAUNCHCTL / HUB_BUN 不是毒丸，用例可能碰到真实的
    launchd。总闸导入时在 sys 上留了带本进程 pid 的标记（tests/conftest.py 的
    _STATE_ATTR），这里只认它。自测在 tests/gate_selftest/。
    """
    import os
    import sys
    gate = getattr(sys, "_dsh_bot_test_gate", None)
    if isinstance(gate, dict) and gate.get("pid") == os.getpid():
        return
    here = os.path.dirname(os.path.abspath(__file__))
    tests = here
    while os.path.basename(tests) != "tests" and os.path.dirname(tests) != tests:
        tests = os.path.dirname(tests)
    root = os.path.dirname(tests)
    raise RuntimeError(
        "测试总闸（tests/conftest.py）没有加载，拒绝运行本目录的用例。\n"
        "在子目录里面启动 pytest 时总闸不生效，用例可能碰到真实的 launchd。\n"
        "请从仓库根或 tests/ 目录启动，例如：\n"
        "    cd %s && python3 -m pytest %s" % (root, os.path.relpath(here, root)))


_require_total_gate()
del _require_total_gate

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def styles_root(tmp_path, monkeypatch):
    """把 styles.json / 示例图目录整体挪到 tmp_path，返回 styles.json 路径。"""
    from moments import styles_routes

    root = tmp_path / "novelai-skill"
    (root / "assets").mkdir(parents=True)
    styles_path = root / "assets" / "styles.json"
    styles_path.write_text(json.dumps(
        {"active": "default", "active_by_bot": {}, "styles": []}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(styles_routes, "SKILL_ROOT", root)
    monkeypatch.setattr(styles_routes, "STYLES_PATH", styles_path)
    monkeypatch.setattr(styles_routes, "DEFAULT_CONFIG_PATH", root / "assets" / "default_config.json")
    monkeypatch.setattr(styles_routes, "GENERATE_SCRIPT", root / "scripts" / "generate_novelai_image.py")
    monkeypatch.setattr(styles_routes, "SAMPLE_DIR", tmp_path / "samples")
    monkeypatch.setattr(styles_routes, "NOVELAI_ENV", root / ".env.local")
    styles_routes._SAMPLING_IDS.clear()
    return styles_path


@pytest.fixture
def client(styles_root):
    """只挂 styles 蓝图的最小 Flask app（不引 moments 的其它路由与数据库）。"""
    from flask import Flask
    from moments.styles_routes import styles_bp

    app = Flask(__name__, template_folder=str(_ROOT / "moments" / "templates"))
    app.register_blueprint(styles_bp)
    app.config["TESTING"] = True
    # 不用 `with app.test_client()`：那会把请求上下文一直留着，并发用例里两个线程
    # 同时在飞时会互相顶掉上下文栈。这里每个请求各自压栈出栈。
    return app.test_client()


@pytest.fixture
def api(client):
    """把测试客户端包成 (code, body) 的小工具，断言里不用重复解 json。"""
    class _Api:
        def get(self, path):
            r = client.get(path)
            return r.status_code, _body(r)

        def post(self, path, payload=None, **kw):
            r = client.post(path, json=payload) if not kw else client.post(path, **kw)
            return r.status_code, _body(r)

        def delete(self, path):
            r = client.delete(path)
            return r.status_code, _body(r)

    def _body(resp):
        try:
            return resp.get_json()
        except Exception:
            return {"_raw_text": resp.get_data(as_text=True)[:500]}

    return _Api()


@pytest.fixture(autouse=True)
def _no_real_skill_root(monkeypatch):
    """兜底：不让任何用例从外部继承 NOVELAI_SKILL_ROOT 而读到真实 skill 目录。"""
    monkeypatch.delenv("NOVELAI_SKILL_ROOT", raising=False)
