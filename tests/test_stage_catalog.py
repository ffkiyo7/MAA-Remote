from datetime import datetime, timezone

import pytest

from maa_remote.models import StageInfo
from maa_remote.stage_catalog import (
    format_menu,
    hot_update,
    load_open_stages,
    resolve_selection,
)


FIX = "tests/fixtures/stage_activity_sample.json"
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_load_open_stages_filters_by_time():
    stages = load_open_stages(FIX, "Official", now=NOW)
    codes = [stage.code for stage in stages]
    assert codes == ["TT-8", "TT-7"]
    assert stages[0].activity_name == "测试当期"
    assert stages[0].drop == "本关效率最高"


def test_load_open_stages_empty_when_none_open():
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert load_open_stages(FIX, "Official", now=past) == []


def test_resolve_selection_by_index():
    stages = [StageInfo("a", "TT-8", "d", "x"), StageInfo("a", "TT-7", "d", "x")]
    assert resolve_selection("1", stages) == "TT-8"
    assert resolve_selection("2", stages) == "TT-7"


def test_resolve_selection_by_code_and_cancel_and_miss():
    stages = [StageInfo("a", "TT-8", "d", "x")]
    assert resolve_selection("tt-8", stages) == "TT-8"
    assert resolve_selection("取消", stages) == "__cancel__"
    assert resolve_selection("99", stages) is None
    assert resolve_selection("ZZ-9", stages) is None


def test_format_menu_lists_stages():
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    menu = format_menu(stages)
    assert "TT-8" in menu and "本关效率最高" in menu and "1" in menu


def test_hot_update_invokes_maa_cli():
    calls = []
    hot_update("C:/maa.exe", runner=lambda cmd, **kw: calls.append((cmd, kw)))
    assert calls[0][0] == ["C:/maa.exe", "hot-update"]
    assert calls[0][1]["timeout"] == 120


def test_hot_update_raises_on_nonzero_exit():
    class Result:
        returncode = 1
        stderr = "failed"

    with pytest.raises(RuntimeError, match="failed"):
        hot_update("C:/maa.exe", runner=lambda cmd, **kw: Result())
