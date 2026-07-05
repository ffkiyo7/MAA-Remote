import shutil

from maa_remote.config import load_config
from maa_remote.models import Fight, TaskPlan
from maa_remote.preview import plan_preview


def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(
        str(tmp_path / "config.toml"),
        env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"},
    )


def test_daily_preview_lists_all_modules_and_footer(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    text = plan_preview(plan, cfg)
    for kw in ["📋", "公招", "基建", "信用商店", "奖励", "只吃快过期的药", "不动囤药", "不碎石", "取消"]:
        assert kw in text, kw
    assert "10 分钟" in text


def test_spend_plan_preview_shows_warnings(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True, stage="UR-8", medicine=2, stone=5))
    plan.recruit.enable = False
    plan.infrast.enable = False
    plan.mall.enable = False
    plan.award.enable = False
    text = plan_preview(plan, cfg)
    assert "⚠️ 动用 2 瓶囤积理智药" in text
    assert "⚠️ 碎 5 颗源石" in text
    assert "UR-8" in text
    assert "公招" not in text


def test_fight_times_and_default_stage_wording(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True, stage="", times=3))
    text = plan_preview(plan, cfg)
    assert "最多 3 次" in text
    assert "上次/当前关卡" in text
