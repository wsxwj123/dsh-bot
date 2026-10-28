# -*- coding: utf-8 -*-
"""dsh 参数页经 17990 适配服务暴露的验收（黑盒）。

依据只有 `.devflow/BRIEF-dsh-params-page.md` 与 `.devflow/INTERFACE-dsh-params-page.md`。
只通过 127.0.0.1:<临时端口> 的 HTTP 接口验证：起真实 `python3 -m moments.adapter_service`，
不 import 被测模块内部，不 mock 内部层，不断言内部状态。

运行：在仓库根目录
    python3 -m pytest tests/acceptance/dsh_params_page -q

注意：本目录是验收先行——实现上线前大部分用例应为红；红表示接口约定未被满足。
"""
import json
import os
import re
import shutil
import time

import pytest
import yaml

#: 一个合法的 dsh 全局配置：两个静默阈值各取一个可辨认的值。
GLOBAL_YML = "silence_threshold_minutes: 42\nmoments:\n  silence_threshold_minutes: 17\n"
TOP_KEY = "silence_threshold_minutes"
NESTED_KEY = "moments.silence_threshold_minutes"

#: 40 位无前缀假密钥：只许存在于测试磁盘，绝不许回显到响应或日志。
SECRET40 = "FAKEKEY-0123456789abcdef0123456789abcd"

UNAUTHORIZED = {"ok": False, "code": "unauthorized", "message": "令牌缺失或不对"}
GENERIC_NOT_FOUND = {"ok": False, "code": "not_found", "message": "没有这个接口"}
METHOD_NOT_ALLOWED = {"ok": False, "code": "method_not_allowed",
                      "message": "这个接口不支持这种请求方式"}

#: 七个入口：(方法, 路径)；401/405 矩阵共用。
ENDPOINTS = [
    ("GET", "/hub/params"),
    ("GET", "/hub/api/params"),
    ("POST", "/hub/api/params"),
    ("GET", "/hub/api/params/raw"),
    ("POST", "/hub/api/params/raw"),
    ("GET", "/hub/api/params/backups"),
    ("POST", "/hub/api/params/backups/20261029-021500/restore"),
]

LINE_NUMBER_RE = re.compile(r"第\s*\d+\s*行")
VERSION_RE = re.compile(r"^\d+-\d+$")


# ---------------------------------------------------------------- 通用小件

def assert_json(resp):
    assert resp.status != 0, "没有拿到 HTTP 响应"
    assert "json" in resp.headers.get("content-type", ""), \
        "响应不是 JSON：状态 %s，Content-Type=%r，开头 %r" % (
            resp.status, resp.headers.get("content-type"), resp.text[:200])
    body = resp.json
    assert isinstance(body, dict), "JSON 响应体必须是对象，实际 %r" % (body,)
    return body


def assert_generic_error(resp, status, expected):
    assert resp.status == status, "期望 %d，实际 %d，体 %r" % (status, resp.status, resp.text[:300])
    assert assert_json(resp) == expected, "错误响应体不对：%r" % (resp.json,)


def assert_handler_error(resp, status, code):
    assert resp.status == status, "期望 %d，实际 %d，体 %r" % (status, resp.status, resp.text[:300])
    body = assert_json(resp)
    assert body.get("error") == code, "错误码应为 %r，实际 %r" % (code, body.get("error"))
    detail = body.get("detail")
    assert isinstance(detail, str) and detail, "detail 必须是非空字符串，实际 %r" % (detail,)
    return body


def allowed_methods(resp):
    raw = resp.headers.get("allow", "")
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


#: 模板里的地址常被 JS 语法包着出现：`'/hub/api/params'`、`footer('/hub/api/params');`
#: 正则会把尾部的引号、括号、分号一起吞进来，提取后要剥掉这些语法字符。
JS_SYNTAX_CHARS = "\"'`);,]} \t\r\n"


def api_urls_in(html):
    """抽出 HTML 里所有 /hub/api/... 形式的地址（去掉 JS 语法字符、查询串与锚点）。"""
    pattern = re.compile(r"/hub/api/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]*")
    urls = []
    for match in pattern.finditer(html):
        url = match.group(0).split("?")[0].split("#")[0].strip(JS_SYNTAX_CHARS)
        if url:
            urls.append(url)
    return urls


def whitelisted_url(url):
    """按适配服务名单判定：五个 params 登记项；<bid>/<job_id> 只占一段。

    参数页复用的模板里还会调既有的 `/hub/api/bots/restart`（适配服务名单里本来就有），
    JS 里用 `'/hub/api/bots/restart/' + encodeURIComponent(jobId)` 拼接，按前缀放行。
    """
    if url in ("/hub/api/params", "/hub/api/params/raw", "/hub/api/params/backups"):
        return True
    if re.fullmatch(r"/hub/api/params/backups/[^/]+/restore", url):
        return True
    # 页面若用 JS 拼接还原地址，源码里会出现名单内接口的前缀，按前缀放行。
    if url in ("/hub/api/params/backups/", "/hub/api/params/backups"):
        return True
    # 既有的重启接口（页面重启按钮用），名单里本就有。
    if url in ("/hub/api/bots/restart", "/hub/api/bots/restart/"):
        return True
    if re.fullmatch(r"/hub/api/bots/restart/[^/]+", url):
        return True
    return False


def schema_strings(node):
    """把 schema 里的键和字符串值全收集起来（schema 形状不写死）。"""
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str):
                    found.add(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            found.add(value)

    walk(node)
    return found


def config_path(service):
    return service.config_dir / "_global.yml"


def seed_config(service, text=GLOBAL_YML):
    path = config_path(service)
    path.write_text(text, encoding="utf-8")
    return path


def config_bytes(service):
    return config_path(service).read_bytes()


def read_config(service):
    return yaml.safe_load(config_bytes(service))


def backup_files(service):
    return sorted(service.config_dir.glob("_global.yml.bak.*"))


def pre_restore_files(service):
    return sorted(service.config_dir.glob("_global.yml.pre-restore.*"))


def get_params(service):
    resp = service.request("GET", "/hub/api/params")
    assert resp.status == 200, "GET /hub/api/params 期望 200，实际 %d：%r" % (
        resp.status, resp.text[:300])
    return assert_json(resp)


def current_version(service):
    return get_params(service)["version"]


def save_values(service, values, version=None):
    if version is None:
        version = current_version(service)
    return service.request("POST", "/hub/api/params",
                           body={"values": values, "version": version})


def restore(service, bid, body):
    return service.request("POST", "/hub/api/params/backups/%s/restore" % bid, body=body)


# ---------------------------------------------------------------- 页面

def test_页面_带令牌恒200_HTML里的每个API地址都在白名单(service):
    """用例不写任何配置文件：页面不读配置目录也必须是 200。"""
    resp = service.request("GET", "/hub/params")
    assert resp.status == 200, "页面期望 200，实际 %d：%r" % (resp.status, resp.text[:300])
    assert resp.headers.get("content-type", "").startswith("text/html"), \
        "页面 Content-Type 应为 text/html，实际 %r" % (resp.headers.get("content-type"),)
    html = resp.text
    assert "<!-- console-nav -->" in html, "导航条位置应有 <!-- console-nav --> 占位"
    urls = api_urls_in(html)
    assert urls, "参数页 HTML 里应至少出现一个 /hub/api/... 地址"
    outside = sorted({url for url in urls if not whitelisted_url(url)})
    assert outside == [], "页面里出现了白名单之外的地址：%r" % (outside,)


# ---------------------------------------------------------------- 鉴权

@pytest.mark.parametrize("auth", [None, "Bearer WRONG-TOKEN-0000"], ids=["不带令牌", "令牌不对"])
@pytest.mark.parametrize("method,path", ENDPOINTS,
                         ids=["%s %s" % (m, p) for m, p in ENDPOINTS])
def test_鉴权_七个入口不带或带错令牌一律401_且不碰文件(service, method, path, auth):
    seed_config(service)
    before_config = config_bytes(service)
    resp = service.request(method, path, body={} if method == "POST" else None, auth=auth)
    assert_generic_error(resp, 401, UNAUTHORIZED)
    assert "location" not in resp.headers, "401 不应带 Location"
    assert "hub-gate" not in resp.headers, "401 不应带 Hub-Gate"
    # 反向：未授权不得写盘、不得产生备份，令牌也不能进日志。
    assert config_bytes(service) == before_config, "401 请求改动了配置文件"
    assert backup_files(service) == [] and pre_restore_files(service) == [], "401 请求产生了备份"
    assert service.read_token() not in service.log_text(), "日志里出现了适配令牌"


def test_鉴权_令牌文件每次请求现读_轮换后旧令牌401新令牌200(service):
    seed_config(service)
    old_token = service.read_token()
    assert service.request("GET", "/hub/api/params").status == 200

    new_token = "ROTATED-TOKEN-0f1e2d3c4b5a6978"
    service.token_path.write_text(new_token, encoding="utf-8")

    old = service.request("GET", "/hub/api/params", auth="Bearer " + old_token)
    assert_generic_error(old, 401, UNAUTHORIZED)
    fresh = service.request("GET", "/hub/api/params")
    assert fresh.status == 200, "新令牌应立刻生效，实际 %d：%r" % (fresh.status, fresh.text[:300])

    # 令牌文件读不到或为空：不管请求头写什么，一律 401。
    service.token_path.unlink()
    gone = service.request("GET", "/hub/api/params", auth="Bearer " + new_token)
    assert_generic_error(gone, 401, UNAUTHORIZED)
    service.token_path.write_text("", encoding="utf-8")
    empty = service.request("GET", "/hub/api/params", auth="Bearer " + new_token)
    assert_generic_error(empty, 401, UNAUTHORIZED)
    service.token_path.write_text(new_token, encoding="utf-8")
    assert service.request("GET", "/hub/api/params").status == 200


def test_鉴权先于路径_名单外路径无令牌也回401(service):
    resp = service.request("GET", "/hub/not-in-the-list", auth=None)
    assert_generic_error(resp, 401, UNAUTHORIZED)


def test_鉴权先于读体_无令牌的超大请求体回401而不是413(service):
    # 头部声明 2 MiB，实际只发 64 KiB：鉴权若先于读体，服务应立即回 401 并断开。
    resp = service.raw_exchange("POST", "/hub/api/params", body=b"x" * 65536,
                                declared_length=2 * 1024 * 1024, auth=None)
    assert resp.status == 401, "期望 401，实际 %d：%r" % (resp.status, resp.body[:300])
    assert json.loads(resp.body) == UNAUTHORIZED


# ---------------------------------------------------------------- 读参数

def test_读参数_返回两个静默阈值_schema两行齐全_version是磁盘指纹(service):
    seed_config(service)
    body = get_params(service)
    assert body["values"][TOP_KEY] == 42
    assert body["values"][NESTED_KEY] == 17
    strings = schema_strings(body["schema"])
    assert TOP_KEY in strings, "schema 里缺 %r 这一行" % TOP_KEY
    assert NESTED_KEY in strings, "schema 里缺 %r 这一行" % NESTED_KEY
    assert VERSION_RE.fullmatch(body["version"]), \
        "version 应为 '<mtime_ns>-<st_size>'，实际 %r" % (body["version"],)
    assert isinstance(body["advanced_available"], bool)
    assert isinstance(body["warnings"], list)


def _corrupt_config(service):
    seed_config(service, "silence_threshold_minutes: 42\nbroken: [unclosed\n")


def _list_top_level(service):
    seed_config(service, "- 1\n- 2\n")


@pytest.mark.parametrize("prepare,version_expected", [
    (lambda svc: None, "absent"),
    (_corrupt_config, "fingerprint"),
    (_list_top_level, "fingerprint"),
], ids=["文件不在", "YAML坏", "顶层不是映射"])
def test_读参数_文件不可读也200_回落默认并给警告(service, prepare, version_expected):
    prepare(service)
    body = get_params(service)
    assert body["advanced_available"] is False, "不可读时应报 advanced_available:false"
    assert "global_yml_unreadable" in body["warnings"], \
        "warnings 应含 global_yml_unreadable，实际 %r" % (body["warnings"],)
    for key in (TOP_KEY, NESTED_KEY):
        value = body["values"][key]
        assert isinstance(value, (int, float)) and not isinstance(value, bool), \
            "回落默认值应是数字，%r=%r" % (key, value)
    if version_expected == "absent":
        assert body["version"] == "absent"
    else:
        assert VERSION_RE.fullmatch(body["version"]), \
            "文件存在时 version 仍是磁盘指纹，实际 %r" % (body["version"],)


# ---------------------------------------------------------------- 保存

def test_保存_写盘改值且产生save备份(service):
    seed_config(service)
    old_version = current_version(service)
    resp = save_values(service, {TOP_KEY: 30})
    assert resp.status == 200, "保存期望 200，实际 %d：%r" % (resp.status, resp.text[:300])
    body = assert_json(resp)
    assert body["ok"] is True
    assert body["changed"] == [TOP_KEY], "changed 应只含改动的键，实际 %r" % (body["changed"],)
    assert isinstance(body["backup"], str) and body["backup"], "应产生备份 id"
    assert set(body["restart_hint"]) == {"bot", "moments_web"}
    assert all(isinstance(v, bool) for v in body["restart_hint"].values())
    assert body["version"] != old_version, "保存后 version 应变化"
    doc = read_config(service)
    assert doc[TOP_KEY] == 30, "磁盘上的顶层阈值应变成 30"
    assert doc["moments"][TOP_KEY] == 17, "未提交的嵌套阈值必须原样保留"
    backup = service.config_dir / ("_global.yml.bak." + body["backup"])
    assert backup.exists(), "备份文件 %s 不存在" % backup.name
    if os.name != "nt":
        assert (backup.stat().st_mode & 0o777) == 0o600, "备份文件应为 0600"


@pytest.mark.parametrize("values", [{}, {TOP_KEY: 42, NESTED_KEY: 17}],
                         ids=["空values", "与磁盘同值"])
def test_保存_无变化时200_changed空_backup为null_不写盘(service, values):
    seed_config(service)
    before = config_bytes(service)
    version = current_version(service)
    resp = save_values(service, values, version)
    assert resp.status == 200, "无变化保存期望 200，实际 %d：%r" % (resp.status, resp.text[:300])
    body = assert_json(resp)
    assert body["ok"] is True
    assert body["changed"] == []
    assert body["backup"] is None, "无变化时不应产生备份，实际 %r" % (body["backup"],)
    assert body["version"] == version, "无变化时 version 不应变"
    assert config_bytes(service) == before, "无变化时文件字节不得改动"
    assert backup_files(service) == [], "无变化时不应产生 .bak"


def test_保存_版本过期409_config_stale且不写盘(service):
    seed_config(service)
    stale_version = current_version(service)
    seed_config(service, GLOBAL_YML + "# 外部又改了一笔\n")
    before = config_bytes(service)
    resp = save_values(service, {TOP_KEY: 30}, version=stale_version)
    assert_handler_error(resp, 409, "config_stale")
    assert config_bytes(service) == before, "409 之后文件不得被改动"
    assert backup_files(service) == [], "409 之后不得产生备份"


def test_保存_同一version重复提交_第二次409且不重复备份(service):
    seed_config(service)
    version = current_version(service)
    request_body = {"values": {TOP_KEY: 30}, "version": version}
    first = service.request("POST", "/hub/api/params", body=request_body)
    assert first.status == 200
    after_first = config_bytes(service)
    backups_after_first = [path.name for path in backup_files(service)]
    assert len(backups_after_first) == 1

    replay = service.request("POST", "/hub/api/params", body=request_body)
    assert_handler_error(replay, 409, "config_stale")
    assert config_bytes(service) == after_first, "重放失败后文件不得再变"
    assert [path.name for path in backup_files(service)] == backups_after_first, \
        "重放失败不得再产生备份"


def test_保存_磁盘YAML坏409_global_yml_unreadable(service):
    _corrupt_config(service)
    before = config_bytes(service)
    resp = save_values(service, {TOP_KEY: 30})
    assert_handler_error(resp, 409, "global_yml_unreadable")
    assert config_bytes(service) == before
    assert backup_files(service) == []


# ---------------------------------------------------------------- 保存的输入校验

BAD_BODY_CASES = [
    ("params体不是JSON", "/hub/api/params", b"not json", None),
    ("params体是数组", "/hub/api/params", None, []),
    ("params缺version", "/hub/api/params", None, {"values": {}}),
    ("params缺values", "/hub/api/params", None, {"version": "x"}),
    ("params的values不是对象", "/hub/api/params", None, {"values": [], "version": "x"}),
    ("params的version不是字符串", "/hub/api/params", None, {"values": {}, "version": 5}),
    ("raw体不是对象", "/hub/api/params/raw", b"[]", None),
    ("raw缺text", "/hub/api/params/raw", None, {"version": "x"}),
    ("raw缺version", "/hub/api/params/raw", None, {"text": "a: 1\n"}),
    ("raw的text不是字符串", "/hub/api/params/raw", None, {"text": 5, "version": "x"}),
    ("raw的version不是字符串", "/hub/api/params/raw", None, {"text": "a: 1\n", "version": 5}),
]


@pytest.mark.parametrize("path,raw,body", [(p, r, b) for _, p, r, b in BAD_BODY_CASES],
                         ids=[name for name, _, _, _ in BAD_BODY_CASES])
def test_写入口_坏body一律400_bad_body且不写盘(service, path, raw, body):
    seed_config(service)
    before = config_bytes(service)
    resp = service.request("POST", path, body=body if raw is None else None, raw=raw)
    body_json = assert_handler_error(resp, 400, "bad_body")
    assert isinstance(body_json["detail"], str) and body_json["detail"]
    assert config_bytes(service) == before, "坏 body 不得改动配置"
    assert backup_files(service) == [], "坏 body 不得产生备份"


def test_保存_非法字段值400_bad_field且不写盘(service):
    seed_config(service)
    before = config_bytes(service)
    version = current_version(service)
    cases = [
        ("未知键", {"no.such.key": 1}),
        ("类型不符", {TOP_KEY: "三十"}),
        ("类型不符null", {TOP_KEY: None}),
        ("负值越界", {TOP_KEY: -1}),
        ("超大值越界", {TOP_KEY: 10 ** 9}),
    ]
    for name, values in cases:
        resp = save_values(service, values, version)
        assert resp.status == 400, "%s：期望 400，实际 %d：%r" % (name, resp.status, resp.text[:200])
        body = assert_handler_error(resp, 400, "bad_field")
        assert isinstance(body.get("detail"), str) and body["detail"]
    assert config_bytes(service) == before, "bad_field 不得改动配置"
    assert backup_files(service) == [], "bad_field 不得产生备份"


def test_保存_NaN与无穷大400_bad_field(service):
    seed_config(service)
    before = config_bytes(service)
    version = current_version(service)
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = '{"values": {"%s": %s}, "version": "%s"}' % (TOP_KEY, literal, version)
        resp = service.request("POST", "/hub/api/params", raw=raw)
        assert resp.status == 400, "%s：期望 400，实际 %d：%r" % (
            literal, resp.status, resp.text[:200])
        assert_handler_error(resp, 400, "bad_field")
    assert config_bytes(service) == before
    assert backup_files(service) == []


def test_边界_零值与schema公布的下限一致(service):
    """0 是否合法由接口自己公布的 schema 范围决定；接口没给范围就跳过并说明。"""
    seed_config(service)
    data = get_params(service)
    lower = _declared_min(data["schema"], TOP_KEY)
    if lower is None:
        pytest.skip("schema 未公布 %s 的下限，0 的合法性无法从对外接口判断" % TOP_KEY)
    if lower <= 0:
        resp = save_values(service, {TOP_KEY: 0})
        assert resp.status == 200, "schema 下限 %r 允许 0，实际 %d：%r" % (
            lower, resp.status, resp.text[:200])
        assert read_config(service)[TOP_KEY] == 0
    else:
        resp = save_values(service, {TOP_KEY: 0})
        assert_handler_error(resp, 400, "bad_field")


def _declared_min(node, key):
    """在 schema 里找提到 key 的那一行，返回它声明的下限（没有就返回 None）。"""
    found = []

    def walk(value):
        if isinstance(value, dict):
            mentioned = (key in value or value.get("key") == key or value.get("name") == key
                         or any(v == key for v in value.values() if isinstance(v, str)))
            if mentioned:
                for name in ("min", "minimum", "least"):
                    candidate = value.get(name)
                    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                        found.append(candidate)
                        return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)
    return found[0] if found else None


# ---------------------------------------------------------------- 读原文

def test_读原文_返回文本与固定占位符(service):
    seed_config(service)
    resp = service.request("GET", "/hub/api/params/raw")
    assert resp.status == 200, "读原文期望 200，实际 %d：%r" % (resp.status, resp.text[:300])
    body = assert_json(resp)
    assert body["placeholder"] == "__KEEP_EXISTING__"
    assert isinstance(body["sensitive_paths"], list)
    assert VERSION_RE.fullmatch(body["version"]), "version 应为磁盘指纹，实际 %r" % (body["version"],)
    assert yaml.safe_load(body["text"]) == {
        TOP_KEY: 42, "moments": {TOP_KEY: 17}}, "原文应能解析回磁盘上的配置"


def test_读原文_文件不在404_文件坏409且不回显密钥(service):
    resp = service.request("GET", "/hub/api/params/raw")
    assert_generic_error(resp, 404, {"error": "not_found", "detail": "configs/_global.yml 不存在"})

    seed_config(service, "api_key: %s\nbroken: [unclosed\n" % SECRET40)
    resp = service.request("GET", "/hub/api/params/raw")
    body = assert_handler_error(resp, 409, "global_yml_unreadable")
    assert LINE_NUMBER_RE.search(body["detail"]), \
        "YAML 错误 detail 只该给行号，实际 %r" % (body["detail"],)
    assert SECRET40 not in resp.text, "响应回显了配置里的假密钥"
    assert SECRET40 not in service.log_text(), "日志泄露了配置里的假密钥"


# ---------------------------------------------------------------- 写原文

def test_写原文_原样回写加中文注释并产生备份(service):
    seed_config(service)
    version = current_version(service)
    # 接口约定入口 5 第 ⑤ 条：四个必需顶层键齐全才允许写，缺一个就是 400 schema_missing_key。
    # 样本里同时保留两个静默阈值，写入后要能原样读回。
    text = (
        "user_display_name: 测试用户\n"
        "max_calls_per_5h: 100\n"
        "jiwen: {}\n"
        + GLOBAL_YML
        + "# 中文注释：😀 引号 \" ' 反斜杠 \\ 保留原样\n"
    )
    resp = service.request("POST", "/hub/api/params/raw",
                           body={"text": text, "version": version})
    assert resp.status == 200, "写原文期望 200，实际 %d：%r" % (resp.status, resp.text[:300])
    body = assert_json(resp)
    assert body["ok"] is True
    assert isinstance(body["backup"], str) and body["backup"]
    assert body["version"] != version
    assert body["restart_hint"] == {"bot": True, "moments_web": True}
    assert config_bytes(service).decode("utf-8") == text, "raw 写入应按提交文本原样落盘"
    saved = read_config(service)
    assert saved[TOP_KEY] == 42 and saved["moments"][TOP_KEY] == 17, \
        "写回后两个静默阈值必须仍在"
    backup = service.config_dir / ("_global.yml.bak." + body["backup"])
    assert backup.exists(), "写原文也必须先产生备份 %s" % backup.name


def test_写原文_坏YAML400_bad_yaml_只给行号且不写盘(service):
    seed_config(service)
    before = config_bytes(service)
    version = current_version(service)
    cases = [
        "silence_threshold_minutes: 42\napi_key: %s\nbroken: [unclosed\n" % SECRET40,
        "- 1\n- 2\n",
    ]
    for text in cases:
        resp = service.request("POST", "/hub/api/params/raw",
                               body={"text": text, "version": version})
        body = assert_handler_error(resp, 400, "bad_yaml")
        assert LINE_NUMBER_RE.search(body["detail"]), \
            "要只报行号，实际 %r" % (body["detail"],)
        assert SECRET40 not in resp.text, "响应回显了假密钥"
        assert SECRET40 not in service.log_text(), "日志泄露了假密钥"
    assert config_bytes(service) == before, "坏 YAML 不得改动配置"
    assert backup_files(service) == [], "坏 YAML 不得产生备份"


# ---------------------------------------------------------------- 备份列表

def test_备份列表_空目录空数组_新到旧_各留最近两份_目录读不到也200(service):
    resp = service.request("GET", "/hub/api/params/backups")
    assert resp.status == 200
    assert assert_json(resp) == {"backups": []}, "没有备份时应为空数组"

    seed_config(service)
    first = assert_json(save_values(service, {TOP_KEY: 30}))["backup"]
    time.sleep(1.05)        # 备份 id 只到秒；拉开一秒让“新到旧”没有歧义
    second = assert_json(save_values(service, {TOP_KEY: 31}))["backup"]
    listing = assert_json(service.request("GET", "/hub/api/params/backups"))["backups"]
    assert [item["id"] for item in listing] == [second, first], "备份应新到旧排列"
    for item in listing:
        assert {"id", "kind", "size", "ts"} <= set(item), "备份条目字段不全：%r" % (item,)
        assert item["kind"] == "save"
        assert isinstance(item["size"], int) and item["size"] > 0
        assert item["ts"] is not None

    time.sleep(1.05)
    third = assert_json(save_values(service, {TOP_KEY: 32}))["backup"]
    listing = assert_json(service.request("GET", "/hub/api/params/backups"))["backups"]
    assert [item["id"] for item in listing] == [third, second], "save 备份只保留最近两份"
    names = {path.name for path in backup_files(service)}
    assert names == {"_global.yml.bak." + third, "_global.yml.bak." + second}, \
        "磁盘上应只剩最近两份 .bak，实际 %r" % (sorted(names),)

    shutil.rmtree(service.config_dir)
    resp = service.request("GET", "/hub/api/params/backups")
    assert resp.status == 200 and assert_json(resp) == {"backups": []}, \
        "备份目录读不到时应回 200 空数组"


# ---------------------------------------------------------------- 还原

def test_还原_从备份恢复内容并产生pre_restore备份(service):
    seed_config(service)
    save_resp = assert_json(save_values(service, {TOP_KEY: 30}))
    backup_id = save_resp["backup"]
    version = current_version(service)

    resp = restore(service, backup_id, {"version": version})
    assert resp.status == 200, "还原期望 200，实际 %d：%r" % (resp.status, resp.text[:300])
    body = assert_json(resp)
    assert body["ok"] is True
    assert isinstance(body["backup"], str) and body["backup"]
    assert body["version"] != version
    assert body["restart_hint"] == {"bot": True, "moments_web": True}
    assert read_config(service)[TOP_KEY] == 42, "还原后应回到备份里的 42"
    pre_restore = service.config_dir / ("_global.yml.pre-restore." + body["backup"])
    assert pre_restore.exists(), "还原前应产生 %s" % pre_restore.name
    assert yaml.safe_load(pre_restore.read_text(encoding="utf-8"))[TOP_KEY] == 30, \
        "pre-restore 备份里应是还原前的 30"
    listing = assert_json(service.request("GET", "/hub/api/params/backups"))["backups"]
    restored = [item for item in listing if item["id"] == body["backup"]]
    assert restored and restored[0]["kind"] == "restore", \
        "还原产生的备份在列表里应是 kind=restore，实际 %r" % (listing,)


def test_还原_body校验先于bid_坏body配畸形bid仍是400_bad_body(service):
    seed_config(service)
    resp = restore(service, "nope", {})
    assert_handler_error(resp, 400, "bad_body")
    resp = service.request("POST", "/hub/api/params/backups/nope/restore", raw=b"not json")
    assert_handler_error(resp, 400, "bad_body")


@pytest.mark.parametrize("bid", [
    "nope", "2026-10-29", "20261029-021500-01-02", "20261029-021500",
    "..%2F..%2Fetc%2Fpasswd", "%E5%A4%87%E4%BB%BD%F0%9F%98%80",
], ids=["乱写", "日期格式不对", "段数太多", "形状对但不存在", "编码的目录穿越", "编码的中文与emoji"])
def test_还原_畸形或不存在的备份404_backup_not_found且无副作用(service, bid):
    seed_config(service)
    before = config_bytes(service)
    resp = service.request("POST", "/hub/api/params/backups/%s/restore" % bid,
                           body={"version": current_version(service)})
    assert_handler_error(resp, 404, "backup_not_found")
    assert config_bytes(service) == before, "404 不得改动配置"
    assert pre_restore_files(service) == [], "404 不得产生备份"


def test_还原_版本过期409_config_stale且不写盘(service):
    seed_config(service)
    backup_id = assert_json(save_values(service, {TOP_KEY: 30}))["backup"]
    stale_version = current_version(service)
    seed_config(service, GLOBAL_YML + "# 外部又改了一笔\n")
    before = config_bytes(service)
    resp = restore(service, backup_id, {"version": stale_version})
    assert_handler_error(resp, 409, "config_stale")
    assert config_bytes(service) == before
    assert pre_restore_files(service) == []


def test_还原_备份内容坏400_bad_yaml且不写盘(service):
    seed_config(service)
    bad_backup_id = "20260101-000000"
    bad_backup = service.config_dir / ("_global.yml.bak." + bad_backup_id)
    bad_backup.write_text("api_key: %s\nbroken: [unclosed\n" % SECRET40, encoding="utf-8")
    before = config_bytes(service)
    resp = restore(service, bad_backup_id, {"version": current_version(service)})
    body = assert_handler_error(resp, 400, "bad_yaml")
    assert LINE_NUMBER_RE.search(body["detail"])
    assert SECRET40 not in resp.text and SECRET40 not in service.log_text()
    assert config_bytes(service) == before
    assert pre_restore_files(service) == []


# ---------------------------------------------------------------- 方法与路径

WRONG_METHOD_CASES = [
    ("POST", "/hub/params", {"GET"}),
    ("PUT", "/hub/api/params", {"GET", "POST"}),
    ("PUT", "/hub/api/params/raw", {"GET", "POST"}),
    ("POST", "/hub/api/params/backups", {"GET"}),
    ("GET", "/hub/api/params/backups/20261029-021500/restore", {"POST"}),
    ("HEAD", "/hub/api/params", {"GET", "POST"}),
    ("OPTIONS", "/hub/api/params", {"GET", "POST"}),
]


@pytest.mark.parametrize("method,path,allow", WRONG_METHOD_CASES,
                         ids=["%s %s" % (m, p) for m, p, _ in WRONG_METHOD_CASES])
def test_错方法405_带Allow头(service, method, path, allow):
    resp = service.request(method, path)
    assert resp.status == 405, "期望 405，实际 %d：%r" % (resp.status, resp.text[:300])
    assert allowed_methods(resp) == allow, "Allow 应为 %r，实际 %r" % (
        allow, resp.headers.get("allow"))
    if method != "HEAD":
        assert assert_json(resp) == METHOD_NOT_ALLOWED


NOT_FOUND_PATHS = [
    "/hub/api/params/nope",
    "/hub/api/params/",
    "/hub/api//params",
    "/hub/api/params%2Fraw",
    "/hub/api/params/../params",
    "/hub/params/",
    "/hub/healthz",
]


@pytest.mark.parametrize("path", NOT_FOUND_PATHS)
def test_名单外路径404_统一形状(service, path):
    resp = service.request("GET", path)
    assert_generic_error(resp, 404, GENERIC_NOT_FOUND)
    assert "location" not in resp.headers


def test_超大请求体413_统一形状无detail(service):
    # 体积闸看的是声明的请求体长度；只发 1 KiB 避免服务端不读体就回包时把连接弄脏。
    resp = service.raw_exchange("POST", "/hub/api/params", body=b"x" * 1024,
                                declared_length=1048576 + 1)
    assert resp.status == 413, "期望 413，实际 %d：%r" % (resp.status, resp.body[:300])
    assert json.loads(resp.body) == {"error": "payload_too_large"}, \
        "413 只许这一种形状，实际 %r" % (resp.body[:300],)
