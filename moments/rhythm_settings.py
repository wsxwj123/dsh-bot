# -*- coding: utf-8 -*-
"""情绪与节奏（约定 5.24）：适配服务 ``GET`` / ``PUT /adapter/v1/bots/{bot_id}/rhythm`` 背后的逻辑。

依据 ``.devflow/dsh-rhythm-INTERFACE.md``（下文节号照它）。适配服务只管鉴权、路由、读请求体，
调这里的 ``check_body`` / ``view`` / ``save``；失败一律抛 ``life_settings.LifeError``。

- 改的文件与生活设定（5.6）是同一份：只经 ``life_settings.locate`` 定位（允许目录、普通文件、非软链、
  非硬链、不与别的 bot 共用）。锁（``life.lock``，与生活设定 PUT 共用一把）、备份
  （``backups/rhythm/<bot_id>/``，只留 2 份）、原子替换全部复用 ``life_settings``。
- 情绪 24 行的标签与单位取 dsh 参数页的 ``hub_config.FIELD_META``；范围取 ``jiwen.merge``，行为 8 行
  取 ``rhythm_config``——消费方读值校验的也是这两张表，页面允许写的值消费方必认。
- GET 的 ``global`` 读 ``config_sources.dsh_dir()/_global.yml``（bot yml 所在的同一目录），不读
  ``HUB_CONFIGS_DIR``：适配服务进程里后者故意指向不存在的目录，``config_loader.load_global()`` 会读到 ``{}``。
  这与消费方读的是同一份全局，靠的是部署约定（console 的 ``HUB_CONFIGS_DSH_DIR`` = 网关给子进程的
  ``HUB_CONFIGS_DIR`` = ``<DSH_BOT_HOME>/configs``：``gateway/src/life/actions.ts`` 的 ``env()`` 设
  ``dirname(--config)``，``autostart.ts`` 设 ``<root>/configs``），见 PLAN ① 问题 1、⑤ R1b。
- 写只改被设 / 被删的那几行，其余字节（注释、空行、键序、别的键的写法）不动；写法认不出的一律
  422，不猜。写后 ``yaml.safe_load(新文)`` 必须等于「原 dict 套上本次改动」，否则 500、文件不动。
- 「父节缺失时补块」「删叶子并删空父节」「按预期 dict 自检」三段的做法照搬自
  claudebotlife ``bot_rhythm.py @5f0b374``（``_insert_mapping_with_leaf`` / ``_delete_scalar`` +
  ``_drop_empty_parents`` / ``_expected_set`` + ``_expected_del``），改写到 dsh 的行级原语上：
  定位改用节点区间，并把流式、锚点别名、重复键、多行值这些认不出的写法统一拦成 422。
- 报错与响应里不放路径、文件原文、PyYAML 的原话。
"""
import copy
import math
import os
import re
import stat

import yaml

import rhythm_config
from jiwen import engine
from jiwen import merge as jiwen_merge
from moments import config_sources, hub_config
from moments import life_settings as ls
from moments.life_settings import LifeError

GLOBAL_YML = "_global.yml"

# 文案（INTERFACE 3.2 / 3.3 逐字）
BODY_SHAPE = "请求体必须带 set 对象、remove 列表与 version 字符串"
KEY_NOT_TEXT = "键必须是文字"
SET_AND_REMOVE = "同一项不能既设置又删除"
NOT_HERE = "不能在这里改"
NEED_INT = "要填整数"
NEED_NUMBER = "要填数字"
NEED_FINITE = "要填有限的数字"
OUT_OF_RANGE = "超出允许范围 [%s, %s]"
UNRECOGNIZED = "这一项的写法认不出，请先手工整理"
STALE = "这只 bot 的节奏刚被别处改过，请刷新后再改"
SELF_CHECK_FAILED = "写回校验没通过，文件没有改动"

# 行级编辑只认这一种换行：别的换行符（YAML 也认 \x85 \u2028 \u2029）会让解析器的行号与按 \n 切的行对不上
_ODD_BREAKS = ("\r", "\x85", "\u2028", "\u2029")
_EMPTY_VALUE = ("", "null", "Null", "NULL", "~")


# ---------------------------------------------------------------- 32 行的表


def _rows():
    """逻辑键 → 行定义（schema 去掉 global 两项）。顺序：21 速率（FIELD_META 顺序）、3 阈值、8 行为。"""
    rows = {}
    rate_defaults = engine.Rates()
    for key, meta in hub_config.FIELD_META.items():
        if key.startswith("jiwen.rates.") and key[len("jiwen.rates."):] in jiwen_merge.RATE_BOUNDS:
            field = key[len("jiwen.rates."):]
            lo, hi = jiwen_merge.RATE_BOUNDS[field]
            rows[key] = {"key": key, "bot_key": "jiwen.rates_override." + field,
                         "label": meta["label"], "unit": meta["unit"], "type": "float",
                         "default": getattr(rate_defaults, field), "tab": "emotion",
                         "min": lo, "max": hi, "_section": "rates", "_field": field}
    th_defaults = engine.Thresholds()
    for field in ("notice", "consider", "forced"):
        key = "jiwen.thresholds." + field
        meta = hub_config.FIELD_META[key]
        lo, hi = jiwen_merge.THRESHOLD_BOUNDS[field]
        rows[key] = {"key": key, "bot_key": "jiwen.thresholds_override." + field,
                     "label": meta["label"], "unit": meta["unit"], "type": "float",
                     "default": getattr(th_defaults, field), "tab": "emotion",
                     "min": lo, "max": hi, "_section": "thresholds", "_field": field}
    for key, spec in rhythm_config.BEHAVIOR_KEYS.items():
        rows[key] = {"key": key, "bot_key": spec["bot_key"], "label": spec["label"],
                     "unit": spec["unit"], "type": "int", "default": spec["default"],
                     "tab": "behavior", "min": spec["min"], "max": spec["max"]}
    return rows


ROWS = _rows()
_PUBLIC = ("key", "bot_key", "label", "unit", "type", "default")


def _emotion(row):
    return row["tab"] == "emotion"


# ---------------------------------------------------------------- 版本与读取


def version_of(st):
    """不透明版本：``st_ino-st_mtime_ns-st_size``。每次替换都是新 inode，mtime 粒度粗也变（R7）。"""
    return "%d-%d-%d" % (st.st_ino, st.st_mtime_ns, st.st_size)


def _load(path, expect=None):
    """``(原文字节, 顶层映射, 读取时的 fstat)``。与 ``life_settings._load`` 同一套判定与文案，
    多回 fstat：版本必须取自读到这份字节的同一次打开（R8）。"""
    data, cur = ls._read(path, expect)
    if data is None:
        raise ls._internal(ls.UNREADABLE)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ls._internal(ls.UNREADABLE) from None
    try:
        doc = yaml.safe_load(text)
    except Exception as e:                  # noqa: BLE001  YAMLError，以及日期越界这类构造时的 ValueError
        raise ls._internal(ls._yaml_message(e)) from None
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ls._internal(ls.BAD_YAML)
    return data, doc, cur


def _read_global():
    """``(全局配置, 读出来没有)``。不存在、读不出、超 1 MiB、解析不了、顶层不是映射 → ``({}, False)``。只读。"""
    folder = config_sources.dsh_dir()
    if folder is None:
        return {}, False
    try:
        with open(os.path.join(str(folder), GLOBAL_YML), "rb") as f:
            raw = f.read(ls.MAX_FILE + 1)
        if len(raw) > ls.MAX_FILE:
            return {}, False
        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception:                       # noqa: BLE001  读不出与解析不了一视同仁：只记告警
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def _locate(bot, bots):
    """定位只走 ``life_settings.locate``；只把「没写 life_config」提到最前面判。

    INTERFACE 3.2：没写 life_config 一律 501「没有单独的生活设定文件」。``locate`` 先查允许目录，
    允许目录不存在时连没写 life_config 的 bot 也回「不在允许位置」——对没有文件的 bot 这句话不对。
    其余判定（允许目录、软链、硬链、共用、不见了）原样交给 ``locate``。
    """
    raw = bot["cfg"].get("life_config")
    if not isinstance(raw, str) or not raw.strip():
        raise ls._unsupported(ls.NO_FILE)
    return ls.locate(bot, bots)


# ---------------------------------------------------------------- GET（3.2）


def _global_of(row, global_cfg):
    """``(消费方没覆盖时实际用的值, 是不是默认)``：与消费方同一组函数（3.5 全局层）。"""
    if not _emotion(row):
        return rhythm_config.global_value(row["key"], global_cfg)
    jiwen = global_cfg.get("jiwen") if isinstance(global_cfg.get("jiwen"), dict) else {}
    section = jiwen.get(row["_section"]) if isinstance(jiwen.get(row["_section"]), dict) else {}
    if row["_field"] in section:
        got = jiwen_merge._global_number(section[row["_field"]])
        if got is not None:
            return got, False
    return row["default"], True


def _is_scalar(value):
    if isinstance(value, float):
        return math.isfinite(value)          # NaN/Inf 不是 JSON 数字，按非标量处理
    return value is None or isinstance(value, (bool, int, str))


def _valid_override(row, value):
    if not _emotion(row):
        return rhythm_config.bot_value_valid(row["key"], value)
    return jiwen_merge._bot_number(value, (row["min"], row["max"])) is not None


def _collect(doc, own):
    """``(逻辑键 → 生效来源的原值, 告警)``。生效来源：bot 自己 yml 盖过生活设定文件（load_bot 浅合并）。"""
    found, warnings = {}, []
    # 情绪：自己 yml 顶层有 jiwen → 整个情绪组以它为准
    if "jiwen" in own:
        warnings.append("bot_yml_wins:jiwen")
        jiwen = own["jiwen"]
    else:
        jiwen = doc.get("jiwen")
    if jiwen is not None and not isinstance(jiwen, dict):
        warnings.append("invalid_section:jiwen")
        jiwen = {}
    for prefix in ("rates", "thresholds"):
        for section in (prefix + "_override", prefix):           # 旧写法后读：两种都在时旧写法生效
            part = (jiwen or {}).get(section)
            if part is None:
                continue
            if not isinstance(part, dict):
                warnings.append("invalid_section:jiwen." + section)
                continue
            for field, value in part.items():
                if prefix == "rates" and field == "immersion_map":
                    warnings.append("not_editable:immersion_map")
                    continue
                key = "jiwen.%s.%s" % (prefix, field)
                if key not in ROWS:
                    warnings.append("not_editable:" + key)
                    continue
                found[key] = value
                if section == prefix:
                    warnings.append("legacy_key:" + key)
    for key, row in ROWS.items():
        if _emotion(row):
            continue
        name = row["bot_key"]
        if name in own:
            warnings.append("bot_yml_wins:" + key)
            found[key] = own[name]
        elif name in doc:
            found[key] = doc[name]
    return found, warnings


def view(bot, bots):
    """GET：``{"bot_id", "version", "schema", "overrides", "warnings"}``。定位或读文件不成立抛 LifeError。"""
    path, st = _locate(bot, bots)
    _data, doc, cur = _load(path, st)
    global_cfg, readable = _read_global()
    schema = []
    for row in ROWS.values():
        item = {k: row[k] for k in _PUBLIC}
        item["global"], item["global_is_default"] = _global_of(row, global_cfg)
        item.update(tab=row["tab"], min=row["min"], max=row["max"])
        schema.append(item)
    found, warnings = _collect(doc, bot["cfg"])
    overrides = {}
    for key, value in found.items():
        if not _is_scalar(value):
            warnings.append("invalid_value:" + key)
            continue
        overrides[key] = value
        if not _valid_override(ROWS[key], value):
            warnings.append("invalid_value:" + key)
    if not readable:
        warnings.append("global_unreadable")
    return {"bot_id": bot["id"], "version": version_of(cur), "schema": schema,
            "overrides": overrides, "warnings": list(dict.fromkeys(warnings))}


# ---------------------------------------------------------------- PUT：请求体与逐键校验（3.3 第 4–6、9 步）


def check_body(set_values, remove_keys, version):
    """第 4–6 步（只看请求体形状，不碰文件）：不成立抛 422。返回 ``(set, 去重后的 remove)``。

    适配服务要在查名单（第 7 步）之前调它；``save`` 自己也会再调一次。
    """
    if not isinstance(set_values, dict) or not isinstance(remove_keys, list) \
            or not isinstance(version, str) or not version:
        raise LifeError(422, "invalid", BODY_SHAPE, fields={})
    if any(not isinstance(k, str) for k in remove_keys):
        raise LifeError(422, "invalid", KEY_NOT_TEXT, fields={})
    remove = list(dict.fromkeys(remove_keys))            # 重复的按一次算，保留第一次出现的位置
    both = [k for k in set_values if k in remove]
    if both:
        raise LifeError(422, "invalid", SET_AND_REMOVE, fields={k: SET_AND_REMOVE for k in both})
    return set_values, remove


def _yml_wins(row, own):
    return ("jiwen" in own) if _emotion(row) else (row["bot_key"] in own)


def _value_error(row, value):
    if row["type"] == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return NEED_INT
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return NEED_NUMBER
        if isinstance(value, float) and not math.isfinite(value):
            return NEED_FINITE
    # 不先转 float：超大的整数转 float 会溢出，直接比较就行
    if not (row["min"] <= value <= row["max"]):
        return OUT_OF_RANGE % (row["min"], row["max"])
    return None


def _field_errors(set_values, remove, own):
    errors = {}
    for key in list(set_values) + remove:
        row = ROWS.get(key)
        if row is None:
            errors[key] = NOT_HERE
        elif _yml_wins(row, own):
            errors[key] = ls.RO_OWN_YML
        elif key in set_values:
            why = _value_error(row, set_values[key])
            if why:
                errors[key] = why
    return errors


# ---------------------------------------------------------------- PUT：行级编辑


class _Unrecognized(Exception):
    """这一项在文件里的写法认不出（3.3 第 13 步）。"""


def _child(node, name):
    """映射里键为 ``name`` 的 ``(键节点, 值节点)``；没有 → None；同名键不止一个 → 认不出。"""
    hits = [(k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == name]
    if len(hits) > 1:
        raise _Unrecognized()
    return hits[0] if hits else None


def _chain(text, root, parts):
    """沿路径走到能走到的最深处，返回 ``[(键节点, 值节点), ...]``；途中写法认不出就抛 ``_Unrecognized``。

    认不出：非空的流式映射（``{a: 1}``）、锚点 / 别名 / 标签（值以 ``& * !`` 开头）、键不在行首、
    路径中途碰到列表或有值的标量（``jiwen: 5``）。
    """
    if root is None:
        return []
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise _Unrecognized()
    lines = text.split("\n")
    chain, cur = [], root
    for depth, part in enumerate(parts):
        if not isinstance(cur, yaml.MappingNode):
            break
        if cur.flow_style:
            if cur.value:
                raise _Unrecognized()
            break                           # 空的 {}：当缺失处理（_set_leaf 改写那一行）
        pair = _child(cur, part)
        if pair is None:
            break
        key, value = pair
        if lines[key.start_mark.line][:key.start_mark.column].strip():
            raise _Unrecognized()
        # 空值（``key:``）的位置标记落在下一个记号上，不能拿来看锚点
        if not _blank(value) and (
                text[value.start_mark.index:value.start_mark.index + 1] in ("&", "*", "!")
                or value.start_mark.index < key.end_mark.index):   # 别名：值节点在键的前面
            raise _Unrecognized()
        chain.append(pair)
        cur = value
        if depth < len(parts) - 1 and not isinstance(value, yaml.MappingNode) and not _empty(value):
            raise _Unrecognized()           # 父节写成了别的东西（jiwen: 5、jiwen: [..]）
    return chain


def _blank(node):
    """``key:`` 后面什么都没写：PyYAML 给的是起止位置相同的空标量。"""
    return isinstance(node, yaml.ScalarNode) and node.start_mark.index == node.end_mark.index


def _empty(node):
    return isinstance(node, yaml.ScalarNode) and node.style is None and node.value in _EMPTY_VALUE \
        or isinstance(node, yaml.MappingNode) and node.flow_style and not node.value


def _render(parts, literal, indent):
    """``a:\\n  b:\\n    leaf: v``，从 ``indent`` 开始、每层两格（照 bot_rhythm._render_nested）。"""
    out = [" " * (indent + 2 * i) + p + ":" for i, p in enumerate(parts[:-1])]
    out.append(" " * (indent + 2 * (len(parts) - 1)) + "%s: %s" % (parts[-1], literal))
    return out


def _last_line(node):
    """这个节点的最后一行内容（行号）；空值回 -1（调用方改用键所在的行）。多行块标量认不出。"""
    if isinstance(node, yaml.ScalarNode):
        if _blank(node):
            return -1
        if node.style in ("|", ">") or node.end_mark.line != node.start_mark.line:
            raise _Unrecognized()
        return node.end_mark.line
    if node.flow_style or not node.value:
        return node.end_mark.line
    last = node.value[-1]
    if isinstance(node, yaml.MappingNode):
        return max(last[0].end_mark.line, _last_line(last[1]))
    return _last_line(last)


_EMPTY_LINE = r"^(\s*)%s\s*:\s*(?:\{\}|null|Null|NULL|~)?\s*(#.*)?$"


def _rewrite_empty(lines, key, name, literal=None, tail=()):
    """``key:`` / ``key: ~`` / ``key: {}`` 那一行改成 ``key:``（给了 ``literal`` 就是 ``key: literal``），
    行尾注释保留，下面接上 ``tail`` 行。这一行不是这种干净写法 → 认不出。"""
    i = key.start_mark.line
    m = re.match(_EMPTY_LINE % re.escape(name), lines[i])
    if not m:
        raise _Unrecognized()
    head = m.group(1) + name + ":" + (" " + literal if literal is not None else "")
    lines[i:i + 1] = [head + ("  " + m.group(2) if m.group(2) else "")] + list(tail)


def _set_leaf(text, parts, literal):
    """把路径 ``parts`` 的叶子写成 ``literal``，返回新文。叶子在就只换值那一段；不在就补出缺的几层。"""
    root = hub_config.compose(text)
    chain = _chain(text, root, parts)
    lines = text.split("\n")
    if len(chain) == len(parts):                                        # 叶子在
        key, value = chain[-1]
        if not isinstance(value, yaml.ScalarNode):
            raise _Unrecognized()
        if _empty(value):
            _rewrite_empty(lines, key, parts[-1], literal=literal)
            return "\n".join(lines)
        if value.style in ("|", ">") or value.end_mark.line != value.start_mark.line \
                or value.start_mark.line != key.start_mark.line:
            raise _Unrecognized()
        return hub_config.splice(text, [(value.start_mark.index, value.end_mark.index, literal)])
    rest = parts[len(chain):]
    if chain:                                                           # 补在已有的那一层下面
        key, value = chain[-1]
        if _empty(value):
            _rewrite_empty(lines, key, parts[len(chain) - 1],
                           tail=_render(rest, literal, key.start_mark.column + 2))
            return "\n".join(lines)
        if not isinstance(value, yaml.MappingNode) or value.flow_style:
            raise _Unrecognized()
        indent = value.value[0][0].start_mark.column
        at = max(_last_line(value), key.start_mark.line) + 1
        lines[at:at] = _render(rest, literal, indent)
        return "\n".join(lines)
    if isinstance(root, yaml.MappingNode) and root.flow_style:         # 顶层是空的 {}
        raise _Unrecognized()
    block = "\n".join(_render(rest, literal, 0)) + "\n"                 # 顶层没有：追加到文件末尾
    if text and not text.endswith("\n"):
        return text + "\n" + block
    return text + block


def _drop_empty_parents(text, parents):
    """删完叶子后，把因此变空的父节键行一并删掉（逐级向上；照 bot_rhythm._drop_empty_parents）。"""
    parents = list(parents)
    while parents:
        root = hub_config.compose(text)
        pair = None
        cur = root
        for part in parents:
            if not isinstance(cur, yaml.MappingNode):
                return text
            pair = _child(cur, part)
            if pair is None:
                return text
            cur = pair[1]
        key, value = pair
        if not _empty(value):
            return text
        lines = text.split("\n")
        i = key.start_mark.line
        if not re.match(_EMPTY_LINE % re.escape(parents[-1]), lines[i]):
            return text
        del lines[i]
        text = "\n".join(lines)
        parents = parents[:-1]
    return text


def _delete_leaf(text, parts):
    """删掉叶子那一整行（值必须与键同一行），变空的父节一并删；本来就没有 → 原样返回。"""
    root = hub_config.compose(text)
    chain = _chain(text, root, parts)
    if len(chain) != len(parts):
        return text
    key, value = chain[-1]
    i = key.start_mark.line
    if not _blank(value) and (value.start_mark.line != i or value.end_mark.line != i):
        raise _Unrecognized()               # 值跨了行（块状列表、多行串）：只删一行会留下残骸
    lines = text.split("\n")
    del lines[i]
    return _drop_empty_parents("\n".join(lines), parts[:-1])


def _expected_set(expected, parts, value):
    """照 bot_rhythm._expected_set：沿路径补出 dict，叶子写值。"""
    cur = expected
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _expected_del(expected, parts):
    """照 bot_rhythm._expected_del：删叶子，因此变空的父 dict 一并删。

    只删「这次删除造成」的空父节：叶子本来就不在时什么都不动。改动前就是 ``{}`` 的父节
    （如 ``rates_override: {}``）文本编辑不会删，预期这边也必须留着，否则写后自检必败（500）。
    """
    cur, stack = expected, []
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        stack.append((cur, part))
        cur = nxt
    if parts[-1] not in cur:
        return
    del cur[parts[-1]]
    for parent, key in reversed(stack):
        if parent[key]:
            break
        parent.pop(key)


def _set_path(row, doc):
    """写哪条路径：情绪键旧写法 ``jiwen.<节>.<f>`` 在文件里就改它（它才生效），否则写 ``*_override``。"""
    if not _emotion(row):
        return [row["bot_key"]]
    jiwen = doc.get("jiwen") if isinstance(doc.get("jiwen"), dict) else {}
    legacy = jiwen.get(row["_section"])
    if isinstance(legacy, dict) and row["_field"] in legacy:
        return ["jiwen", row["_section"], row["_field"]]
    return ["jiwen", row["_section"] + "_override", row["_field"]]


def _remove_paths(row):
    if not _emotion(row):
        return [[row["bot_key"]]]
    return [["jiwen", row["_section"] + "_override", row["_field"]],
            ["jiwen", row["_section"], row["_field"]]]


def _float_literal(value):
    """小数写回 YAML 的字面量：``yaml.safe_load`` 读回必须是同值 float。

    PyYAML 按 YAML 1.1 认浮点：科学计数法必须带小数点、指数必须带正负号。``repr`` 给的 ``5e-05``、
    ``1e+16`` 会被读成字符串，写后自检必败（500）。这里补成 ``5.0e-05``、``1.0e+16``；其余（``0.1``、
    ``1.0``）照 ``repr``。不改 ``hub_config._literal``：参数页也在用它。
    """
    text = repr(float(value))
    if "e" in text:
        mantissa, exponent = text.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        if exponent[:1] not in ("+", "-"):
            exponent = "+" + exponent
        text = mantissa + "e" + exponent
    return text


def _literal(typed, typ):
    return _float_literal(typed) if typ == "float" else hub_config._literal(typed, typ)


def _edit(text, doc, set_values, remove):
    """在内存里改好新文与预期 dict。写法认不出的键收齐了一起报 422（第 13 步）。"""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    expected = copy.deepcopy(doc)
    odd = {}
    for key, value in set_values.items():
        row = ROWS[key]
        parts = _set_path(row, doc)
        typed = int(value) if row["type"] == "int" else float(value)
        try:
            if any(ch in body for ch in _ODD_BREAKS):
                raise _Unrecognized()
            body = _set_leaf(body, parts, _literal(typed, row["type"]))
        except _Unrecognized:
            odd[key] = UNRECOGNIZED
            continue
        _expected_set(expected, parts, typed)
    for key in remove:
        try:
            for parts in _remove_paths(ROWS[key]):
                if any(ch in body for ch in _ODD_BREAKS):
                    raise _Unrecognized()
                body = _delete_leaf(body, parts)
        except _Unrecognized:
            odd[key] = UNRECOGNIZED
            continue
        for parts in _remove_paths(ROWS[key]):
            _expected_del(expected, parts)
    if odd:
        raise LifeError(422, "invalid", ls.INVALID, fields=odd)
    return bom + body, expected


def _self_check(new_text, expected):
    try:
        return yaml.safe_load(new_text) == expected
    except Exception:                       # noqa: BLE001  解析不了同样算没通过
        return False


# ---------------------------------------------------------------- PUT 入口（3.3）


def save(bot, bots, set_values, remove_keys, version):
    """PUT 第 4 步起：成功回 ``{"version", "changed"}``，失败抛 LifeError。

    第 11 到 17 步失败时生活设定文件逐字节不变、不留临时文件；第 12 步及之前失败不建备份。
    新文与原文逐字节相同时不备份、不写盘、版本不变。
    """
    set_values, remove = check_body(set_values, remove_keys, version)   # 第 4–6 步
    path, st = _locate(bot, bots)                                       # 第 8 步
    _data, _doc, cur = _load(path, st)
    errors = _field_errors(set_values, remove, bot["cfg"])              # 第 9 步
    if errors:
        raise LifeError(422, "invalid", ls.INVALID, fields=errors)
    if not set_values and not remove:                                   # 第 10 步
        return {"version": version_of(cur), "changed": []}
    changed = list(set_values) + remove
    with ls._write_lock():                                              # 第 11 步
        data, doc, now = _load(path)            # 锁内重读：版本与改写用同一次读到的字节
        if version_of(now) != version:                                  # 第 12 步
            raise LifeError(409, "stale", STALE)
        if now.st_nlink != 1:                   # 锁外检查之后被人硬链了：同生活设定，改了会连带别处
            raise ls._unsupported(ls.SHARED)
        text = data.decode("utf-8")
        new_text, expected = _edit(text, doc, set_values, remove)       # 第 13 步
        if not _self_check(new_text, expected):                         # 第 14 步
            raise ls._internal(SELF_CHECK_FAILED)
        if new_text == text:
            return {"version": version_of(now), "changed": changed}
        try:
            folder = ls._backup(bot["id"], data, kind="rhythm")         # 第 15 步
        except OSError:
            raise ls._internal(ls.BACKUP_FAILED) from None
        try:
            ls._replace(path, new_text, stat.S_IMODE(now.st_mode))      # 第 16 步
        except OSError:
            raise ls._internal(ls.WRITE_FAILED) from None
        ls._rotate(folder)
        return {"version": version_of(os.lstat(path)), "changed": changed}
