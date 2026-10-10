# -*- coding: utf-8 -*-
"""``disabled_ids_safe`` 的目录级容错。一根坏只跳过那根，别的根照常判定。

两根配置根是混跑期的常态（旧系统 HUB_CONFIGS_DIR + 新系统 HUB_CONFIGS_DSH_DIR）。
一个根读不了就把整个判定作废（返回空集 = 全部当启用），仓库里所有后台生产者会
继续给停用的 bot 投递并拉起它，失败面从一个根扩到全部根。坏根只影响它自己。
"""
import os
import stat
from pathlib import Path

import pytest

import bots_registry


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """两套 tmp 配置根 + 钉住的 HOME；每个用例从"没有告警过"开始。"""
    home = tmp_path / "home"
    legacy = tmp_path / "legacy"
    dsh = tmp_path / "dsh"
    for d in (home, legacy, dsh):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.setattr(bots_registry, "_safe_warned", False)
    return legacy, dsh


def _write_bot(root, name, enabled=None):
    text = "id: %s\n" % name
    if enabled is not None:
        text += "enabled: %s\n" % ("true" if enabled else "false")
    (Path(root) / ("%s.yml" % name)).write_text(text, encoding="utf-8")


def _restore(d):
    os.chmod(d, stat.S_IRWXU)


def test_新根坏旧根好_按旧根判停用(roots):
    """旧根里有停用的 bot2，新根读不了，bot2 必须照样被判停用。"""
    legacy, dsh = roots
    _write_bot(legacy, "bot2", enabled=False)
    os.chmod(dsh, 0)
    try:
        assert bots_registry.disabled_ids_safe() == {"bot2"}
    finally:
        _restore(dsh)


def test_旧根坏新根好_按新根判停用(roots):
    """反过来，坏的是旧根，新根的判定不许被它拖下水。"""
    legacy, dsh = roots
    _write_bot(legacy, "bot9", enabled=False)      # 在坏根里，判不到是正常的
    _write_bot(dsh, "bot5", enabled=False)
    os.chmod(legacy, 0)
    try:
        assert bots_registry.disabled_ids_safe() == {"bot5"}
    finally:
        _restore(legacy)


def test_一根坏不影响另一根的启用判定(roots):
    """好根里"有一份启用就算在跑"的合并口径照旧，坏根里同名 bot 那份不作数。"""
    legacy, dsh = roots
    _write_bot(legacy, "bot2", enabled=False)
    _write_bot(dsh, "bot2", enabled=True)
    os.chmod(legacy, 0)
    try:
        assert bots_registry.disabled_ids_safe() == set()
    finally:
        _restore(legacy)


def test_唯一那个根坏_空集且stderr一次(roots, capsys):
    """只有一个根能判、它又坏了的场合仍是 fail-open（既有验收的语义，不许回归）。"""
    legacy, dsh = roots
    _write_bot(legacy, "bot2", enabled=False)
    os.chmod(legacy, 0)
    os.rmdir(dsh)                                  # 新根不存在（混跑期只会有一边）
    try:
        assert bots_registry.disabled_ids_safe() == set()
        assert bots_registry.disabled_ids_safe() == set()
    finally:
        _restore(legacy)
    err = capsys.readouterr().err
    assert err.count("disabled_ids failed:") == 1, err


def test_两个根都坏_空集(roots):
    legacy, dsh = roots
    _write_bot(legacy, "bot2", enabled=False)
    os.chmod(legacy, 0)
    os.chmod(dsh, 0)
    try:
        assert bots_registry.disabled_ids_safe() == set()
    finally:
        _restore(legacy)
        _restore(dsh)


def test_两个根都好_照常合并判定(roots):
    """正常路径不回归。一边停用、另一边启用，同名算在跑；两边都停才算停。"""
    legacy, dsh = roots
    _write_bot(legacy, "bot2", enabled=False)
    _write_bot(dsh, "bot2", enabled=True)
    _write_bot(legacy, "bot3", enabled=False)
    _write_bot(dsh, "bot3", enabled=False)
    assert bots_registry.disabled_ids_safe() == {"bot3"}
