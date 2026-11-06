# -*- coding: utf-8 -*-
"""life-private-dir 验收的纯工具：固定文案、软链夹具、公开入口的发请求与断言。

依据只有 `.devflow/life-private-dir-接口.md`（下称接口）。
发请求沿用 tests/acceptance/dsh_rhythm/rhythm_util.py 的 World：
配置根里一份 `<bot_id>.yml`，生活设定文件在 `DSH_BOT_HOME/life/<bot_id>.yml`，
`moments.adapter_service.create_app()` 起测试客户端。
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACCEPTANCE_ROOT = HERE.parent
if str(ACCEPTANCE_ROOT) not in sys.path:
    sys.path.insert(0, str(ACCEPTANCE_ROOT))

from dsh_rhythm import rhythm_util as ru  # noqa: E402

PREFIX = "/adapter/v1"

#: 接口「错误契约」逐字文案
LINK_MESSAGE = "锁或备份目录不在预期位置（可能是软链），这次没有继续"

#: 一个能通过 life_settings 校验、也承载节奏覆盖的最小生活设定文件
LIFE_TEXT = "city: 杭州\n"

#: 节奏里的一项行为键（dsh_rhythm 的 schema 用它写生活设定文件）
DAILY = "moments.daily_post_limit_per_bot"


def life_url(bot_id=None):
    return "%s/bots/%s/life" % (PREFIX, bot_id or ru.BOT)


def _files(d):
    d = Path(d)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.name)


class World(ru.World):
    """在 rhythm_util.World 上加生活设定 PUT、life 备份目录、软链夹具。"""

    # -- 公开入口 ----------------------------------------------------------
    def life_put(self, values, bot_id=None):
        return self.call("PUT", life_url(bot_id), {"values": values})

    # -- 盘上状态 ----------------------------------------------------------
    def life_backups(self, bot_id=None):
        return _files(self.sb.dsh_home / "backups" / "life" / (bot_id or ru.BOT))

    def rhythm_backups(self, bot_id=None):
        return _files(self.sb.dsh_home / "backups" / "rhythm" / (bot_id or ru.BOT))

    # -- 造软链（全部落在 tmp 里） -----------------------------------------
    def outside(self, name="outside"):
        p = self.sb.tmp / name
        p.mkdir(exist_ok=True)
        return p

    def make_link(self, name, target):
        os.symlink(str(target), str(self.sb.dsh_home / name))

    # -- 断言 --------------------------------------------------------------
    def expect_private_dir_refused(self, resp):
        """接口错误契约：500 / internal / 逐字文案 / ok false。返回响应体。"""
        body = ru.body_of(resp)
        assert resp.status_code == 500, "应为 500，实际 %s：%r" % (resp.status_code, body)
        assert body.get("ok") is False, "失败响应必有 ok: false：%r" % (body,)
        # 接口写的是 error 字段，适配服务现用的错误体叫 code；两者都认，值必须是 internal
        assert (body.get("code") or body.get("error")) == "internal", \
            "失败码应为 internal，实际 %r（整包 %r）" % (body.get("code") or body.get("error"), body)
        assert body.get("message") == LINK_MESSAGE, \
            "message 应逐字为 %r，实际 %r" % (LINK_MESSAGE, body.get("message"))
        return body
