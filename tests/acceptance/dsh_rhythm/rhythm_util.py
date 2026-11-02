# -*- coding: utf-8 -*-
"""dsh「情绪与节奏」（bot.rhythm）验收的纯工具：契约里的固定取值、摆文件、发请求、通用断言。

依据只有 `.devflow/BRIEF-dsh-rhythm.md` 与 `.devflow/dsh-rhythm-INTERFACE.md`（下称 INTERFACE）。
摆假 bot 的办法借自 tests/acceptance/bot_ops 的现成支架（配置根里一份 `<bot_id>.yml`，
令牌文件经 DSH_CONSOLE_TOKEN_FILE 指到临时目录，`moments.adapter_service.create_app()` 起测试客户端）。
所有编号、令牌都是编的假值。
"""
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

# ---------------------------------------------------------------- 路径
PREFIX = "/adapter/v1"
META = PREFIX + "/meta"


def rhythm_url(bot_id):
    return "%s/bots/%s/rhythm" % (PREFIX, bot_id)


# ---------------------------------------------------------------- 假值
ADAPTER_TOKEN = "FAKE-ADAPTER-TOKEN-rhythm-do-not-leak-3c5e7a9b1d"
WRONG_TOKEN = "FAKE-WRONG-TOKEN-rhythm-never-valid-0a1b2c3d4e"
BOT = "testlulu"
OTHER_BOT = "testyao"
PAST = 1600000000          # 文件修改时间先拨到过去，"没写过"才看得出来

# ---------------------------------------------------------------- INTERFACE 3.1：32 行
#: 情绪速率 21 个字段 → 可写范围（INTERFACE 3.1 的四组）
RATE_RANGES = {}
for _f in ("connection_per_min", "pride_decay_per_min", "valence_decay_per_min",
           "arousal_decay_per_min", "valence_lock_factor", "arousal_connection_rise_rate",
           "pride_defend_rate", "pride_arousal_conflict_rate", "pride_erosion_rate",
           "immersion_decay_per_min", "activity_connection_relief"):
    RATE_RANGES[_f] = (0.0, 1.0)
for _f in ("connection_accel", "valence_severe_multiplier", "valence_mild_multiplier"):
    RATE_RANGES[_f] = (0.0, 5.0)
for _f in ("valence_severe_low", "valence_mild_low", "valence_lock_threshold",
           "arousal_connection_rise_threshold", "pride_defend_threshold", "pride_defend_target"):
    RATE_RANGES[_f] = (-1.0, 1.0)
RATE_RANGES["accel_threshold_min"] = (0.0, 1440.0)

THRESHOLDS = [("notice", 0.2), ("consider", 0.35), ("forced", 0.5)]

#: 行为 8 行，按 INTERFACE 3.1 行为表从上到下：(逻辑键, 文件里的键, min, max, default, label, unit)
BEHAVIOR = [
    ("moments.daily_post_limit_per_bot", "moments_daily_limit", 0, 50, 3,
     "每个 bot 每天发帖上限", "条"),
    ("moments.silence_threshold_minutes", "moments_silence_threshold_minutes", 0, 1440, 30,
     "刚聊完天后多久内不主动发朋友圈（0 = 不设静默期）", "分钟"),
    ("silence_threshold_minutes", "proactive_silence_threshold_minutes", 0, 1440, 30,
     "刚聊完天后多久内不主动找你（0 = 不设静默期）", "分钟"),
    ("moments.min_interval_minutes", "moments_min_interval_minutes", 0, 1440, 60,
     "发圈两条之间至少隔多久（0 = 不设间隔）", "分钟"),
    ("moments.same_kind_hours", "moments_same_kind_hours", 0, 72, 4,
     "同类朋友圈之间至少隔多久（0 = 不设限制）", "小时"),
    ("proactive_max_calls_per_5h", "proactive_max_calls_per_5h", 0, 1000, 0,
     "主动找你每 5 小时最多几次（0 = 不限）", "次"),
    ("proactive_cooldown_min_minutes", "proactive_cooldown_min_minutes", 1, 960, 30,
     "主动消息最短冷却", "分钟"),
    ("proactive_cooldown_max_minutes", "proactive_cooldown_max_minutes", 1, 1440, 1440,
     "主动消息最长冷却", "分钟"),
]
ROW_KEYS = {"key", "bot_key", "label", "unit", "type", "default", "global",
            "global_is_default", "tab", "min", "max"}

#: 改动前的能力表（2026-11-02 在临时目录里实跑 GET /adapter/v1/meta 得到，与 bot_ops 支架的
#: CAPABILITIES_V11 一致）。INTERFACE 3.5 末条：多一项 bot.rhythm，其余项不变。
CAPABILITIES_BEFORE = {
    "bot.restart.graceful": True, "bot.restart.force": True, "bot.set_provider": True,
    "bot.reset_provider": True, "bot.set_effort": True, "provider.subscription": False,
    "provider.per_session": False, "provider.switch_new_segment": True, "bot.notify": True,
    "bot.life_settings": True, "bot.enable": True, "bot.switch_in": True, "bot.group_chat": True,
    "bot.persona": True, "bot.voice": True, "bot.skills": True, "bot.create": True,
    "bot.archive": True, "bot.delete": True, "bot.profile": True, "bot.token_handoff": True,
    "bot.groups_discovered": True, "bot.features": True,
}

# ---------------------------------------------------------------- INTERFACE 逐字文案
UNAUTHORIZED = {"ok": False, "code": "unauthorized", "message": "令牌缺失或不对"}
BAD_BOT_ID = "bot 编号不合规：只能用字母、数字、点、下划线、横线，1 到 64 个字符"
UNKNOWN_BOT = "没有这只 bot"
NO_LIFE = "这只 bot 没有单独的生活设定文件，不能在这里改"
SHARED_LIFE = "这只 bot 的生活设定文件和别处共用，改了会连带别处，不能在这里改"
LIFE_MISSING = "这只 bot 的生活设定文件不见了，请检查它的配置"
LIFE_BAD_YAML = "这只 bot 的生活设定文件格式有错"
STALE = "这只 bot 的节奏刚被别处改过，请刷新后再改"
FIELDS_BAD = "有几项没填对"
SET_AND_REMOVE = "同一项不能既设置又删除"
NOT_HERE = "不能在这里改"
NEED_INT = "要填整数"
NEED_NUMBER = "要填数字"
UNRECOGNIZED = "这一项的写法认不出，请先手工整理"
BOT_YML_WINS = "这只 bot 自己的配置里也写了这一项，并且以那边为准，这里只能看"

BACKUP_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]{2})?\.yml$")

#: 一份带中文注释、空行、行尾注释的生活设定文件（排版保留断言用）
LIFE_SAMPLE = (
    "# 测试露露的生活设定（假数据）\n"
    "life_id: testlulu\n"
    "\n"
    "# 作息：工作日\n"
    "schedule:\n"
    "  - name: 上班   # 行尾注释，不许被动\n"
    "    time: \"09:00-18:00\"\n"
    "\n"
    "events: []\n"
)

DEFAULT_GLOBAL = "user_display_name: 我\n"


# ---------------------------------------------------------------- 文件
def put_file(path, data):
    """写文件（str 按 UTF-8，不做换行转换），修改时间拨到过去。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    os.utime(str(p), (PAST, PAST))
    return p


def read_text(path):
    return Path(path).read_bytes().decode("utf-8")


def file_mark(path):
    """逐字节 + 纳秒修改时间的指纹；文件不在为 None。"""
    p = Path(path)
    if not p.exists():
        return None
    return (p.read_bytes(), p.stat().st_mtime_ns)


def load_yaml(path):
    import yaml
    return yaml.safe_load(read_text(path))


def _q(path):
    """YAML 单引号标量：反斜杠原样保留（Windows 路径不被当成转义）。"""
    return "'%s'" % str(path).replace("'", "''")


# ---------------------------------------------------------------- 发请求与断言
def body_of(resp):
    raw = resp.get_data(as_text=True)
    try:
        body = json.loads(raw)
    except ValueError:
        raise AssertionError("响应体不是 JSON：状态 %s，开头 %r" % (resp.status_code, raw[:200]))
    assert isinstance(body, dict), "响应体不是 JSON 对象：%r" % (body,)
    return body


def expect_ok(resp):
    body = body_of(resp)
    assert resp.status_code == 200, "应为 200，实际 %s：%r" % (resp.status_code, body)
    assert body.get("ok") is True, "成功响应必有 ok: true：%r" % (body,)
    return body


def expect_error(resp, status, code, message=None, prefix=None):
    """失败：状态码、code、message（逐字或前缀）都对得上。返回响应体。"""
    body = body_of(resp)
    assert (resp.status_code, body.get("code")) == (status, code), \
        "应为 %d %s，实际 %d %r" % (status, code, resp.status_code, body)
    assert body.get("ok") is False, "失败响应必有 ok: false：%r" % (body,)
    if message is not None:
        assert body.get("message") == message, "message 应逐字为 %r，实际 %r" % (message, body.get("message"))
    if prefix is not None:
        assert str(body.get("message", "")).startswith(prefix), \
            "message 应以 %r 开头，实际 %r" % (prefix, body.get("message"))
    return body


class World:
    """一条用例的全部家当：配置根、生活设定目录、备份目录、测试客户端。"""

    def __init__(self, sandbox):
        self.sb = sandbox
        self._client = None

    # -- 客户端 ----------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            import importlib
            mod = importlib.import_module("moments.adapter_service")   # 适配服务对外入口
            self._client = mod.create_app().test_client()
        return self._client

    def call(self, method, path, body=None, *, token=ADAPTER_TOKEN, raw=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        kw = {"method": method, "headers": headers}
        if raw is not None:
            kw["data"] = raw
            kw["content_type"] = "application/json"
        elif body is not None:
            kw["json"] = body
        return self.client.open(path, **kw)

    def get(self, bot_id=BOT, **kw):
        return self.call("GET", rhythm_url(bot_id), **kw)

    def put(self, body, bot_id=BOT, **kw):
        return self.call("PUT", rhythm_url(bot_id), body, **kw)

    def version(self, bot_id=BOT):
        v = expect_ok(self.get(bot_id)).get("version")
        assert isinstance(v, str) and v, "version 应是非空字符串：%r" % (v,)
        return v

    # -- 摆文件 ----------------------------------------------------------
    @property
    def life_dir(self):
        return self.sb.dsh_home / "life"

    def life_path(self, bot_id=BOT):
        return self.life_dir / ("%s.yml" % bot_id)

    def global_path(self):
        return self.sb.dsh_cfg / "_global.yml"

    def write_global(self, text):
        return put_file(self.global_path(), text)

    def bot(self, bot_id=BOT, life_text=LIFE_SAMPLE, *, life_path=None, extra_yml="",
            write_life=True):
        """在配置根摆一只 dsh bot。

        life_text=None：yml 里不写 life_config；life_path 给了就指向那份（共用场景）；
        write_life=False：只写 life_config、不建文件（"文件不见了"场景）；
        extra_yml：追加到 bot 自己 yml 顶层的几行。
        """
        botdir = self.sb.dsh_home / "bots" / bot_id
        channel = botdir / "channel"
        channel.mkdir(parents=True, exist_ok=True)
        lines = ["id: %s" % bot_id,
                 "display_name: 测试%s" % bot_id,
                 "bot_channel_path: %s" % _q(channel),
                 "dispatcher_port: 0",
                 "brain:",
                 "  provider: deepseek-official",
                 "  model: deepseek-flash"]
        life = None
        if life_path is not None:
            life = Path(life_path)
        elif life_text is not None:
            life = self.life_path(bot_id)
            if write_life:
                put_file(life, life_text)
        if life is not None:
            lines.append("life_config: %s" % _q(life))
        yml = put_file(self.sb.dsh_cfg / ("%s.yml" % bot_id),
                       "\n".join(lines) + "\n" + extra_yml)
        if not self.global_path().exists():
            self.write_global(DEFAULT_GLOBAL)
        return SimpleNamespace(id=bot_id, yml=yml, life=life)

    def backups(self, bot_id=BOT):
        d = self.sb.dsh_home / "backups" / "rhythm" / bot_id
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.is_file())


def rows_by_key(body):
    return {r["key"]: r for r in body["schema"]}


def capabilities_of(meta_body):
    """meta 回体里装能力表的那个对象（认得出 bot.restart.force 的那一层）。"""
    for v in meta_body.values():
        if isinstance(v, dict) and "bot.restart.force" in v:
            return v
    raise AssertionError("meta 回体里找不到能力表：%r" % (meta_body,))
