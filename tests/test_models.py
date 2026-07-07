from maa_remote.config import FightConfig
from maa_remote.models import TaskPlan


DEF = FightConfig(stage="", expiring_medicine=True, medicine=0, stone=0)


def test_from_llm_dict_applies_fight_defaults():
    plan = TaskPlan.from_llm_dict(
        {"action": "run", "fight": {"enable": True, "stage": "CE-6", "times": 3}},
        DEF,
    )
    assert plan.action == "run"
    assert plan.fight.enable is True
    assert plan.fight.stage == "CE-6"
    assert plan.fight.times == 3
    assert plan.fight.expiring_medicine is True
    assert plan.fight.medicine == 0 and plan.fight.stone == 0
    assert plan.fight.series == 0


def test_from_llm_dict_accepts_fixed_series():
    plan = TaskPlan.from_llm_dict(
        {"action": "run", "fight": {"enable": True, "stage": "CE-6", "series": 6}},
        DEF,
    )
    assert plan.fight.series == 6


def test_from_llm_dict_disables_subtask():
    plan = TaskPlan.from_llm_dict({"action": "run", "recruit": {"enable": False}}, DEF)
    assert plan.recruit.enable is False
    assert plan.infrast.enable is True
    assert plan.startup is True


def test_from_llm_dict_forces_startup_on():
    plan = TaskPlan.from_llm_dict({"action": "run", "startup": False}, DEF)
    assert plan.startup is True


def test_daily_builds_full_plan():
    plan = TaskPlan.daily(DEF, ["startup", "recruit", "infrast", "mall", "award", "fight"])
    assert plan.action == "run"
    assert plan.startup is True
    assert plan.fight.enable is True and plan.fight.expiring_medicine is True
    assert plan.fight.series == 0
    assert plan.recruit.enable and plan.mall.enable and plan.award.enable


def test_daily_fight_controlled_by_task_list():
    plan = TaskPlan.daily(DEF, ["recruit"])
    assert plan.startup is True
    assert plan.fight.enable is False
    assert plan.infrast.enable is False


def test_clarify_carries_question():
    plan = TaskPlan.from_llm_dict(
        {"action": "clarify", "clarify_question": "跑日常还是刷关?"},
        DEF,
    )
    assert plan.action == "clarify"
    assert plan.clarify_question == "跑日常还是刷关?"
