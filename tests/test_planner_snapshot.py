import json

import pytest
from jsonschema import ValidationError, validate

from maa_remote.config import load_config
from maa_remote.models import StageInfo, TaskPlan
from maa_remote.planner_snapshot import (
    PlannerValidationError,
    build_planner_snapshot,
    normalize_stage_key,
    render_advise,
    validate_planner_output,
)


def _cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(open("config.example.toml", encoding="utf-8").read(), encoding="utf-8")
    return load_config(
        str(path),
        env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"},
    )


def _snapshot(tmp_path, stages=None, pending_plan=None):
    cfg = _cfg(tmp_path)
    stages = stages if stages is not None else [StageInfo("测试当期", "TT-8", "酮凝集", "x")]
    return build_planner_snapshot(
        cfg,
        stage_loader=lambda path, client: stages,
        pending_plan=pending_plan,
    )


SCHEMA = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))


def test_snapshot_contains_capabilities_aliases_activity_and_pending_plan(tmp_path):
    pending = TaskPlan.daily(_cfg(tmp_path).maa.fight, ["recruit", "fight"])
    snapshot = _snapshot(tmp_path, pending_plan=pending)
    assert "Fight" in snapshot["maa_capabilities"]["tasks"]
    assert {"name": "钱本", "stage": "CE-6"} in snapshot["aliases"]
    assert snapshot["open_activity_stages"][0]["code"] == "TT-8"
    assert snapshot["pending_plan"]["fight"]["enable"] is True


def test_normalize_stage_key_removes_separators_without_generating_stage_codes():
    assert normalize_stage_key("ce-6") == normalize_stage_key("CE6")
    assert normalize_stage_key("OF-F4") == normalize_stage_key("off4")


def test_validator_allows_alias_stage_and_empty_stage(tmp_path):
    snapshot = _snapshot(tmp_path)
    data = {"action": "run", "fight": {"enable": True, "stage": "ce6"}}
    validate_planner_output(data, snapshot, "刷钱本", "fresh")
    assert data["fight"]["stage"] == "CE-6"
    validate_planner_output(
        {"action": "run", "fight": {"enable": True, "stage": ""}},
        snapshot,
        "刷当前关",
        "fresh",
    )


def test_validator_rejects_run_action_in_confirm_mode(tmp_path):
    snapshot = _snapshot(tmp_path)
    with pytest.raises(PlannerValidationError, match="confirmation mode"):
        validate_planner_output(
            {"action": "run", "fight": {"enable": True, "stage": "CE-6"}},
            snapshot,
            "换成 CE-6",
            "confirm",
        )


def test_validator_allows_stage_explicitly_present_in_original_text(tmp_path):
    snapshot = _snapshot(tmp_path, stages=[])
    validate_planner_output(
        {"action": "run", "fight": {"enable": True, "stage": "OF-F4"}},
        snapshot,
        "打 off4 三次",
        "fresh",
    )


def test_validator_does_not_allow_open_activity_stage_for_execution_by_existence(tmp_path):
    snapshot = _snapshot(tmp_path, stages=[StageInfo("测试当期", "TT-8", "酮凝集", "x")])
    with pytest.raises(PlannerValidationError):
        validate_planner_output(
            {"action": "run", "fight": {"enable": True, "stage": "TT-8"}},
            snapshot,
            "刷当前活动代币",
            "fresh",
        )


def test_validator_uses_original_text_not_assembled_prompt(tmp_path):
    snapshot = _snapshot(tmp_path, stages=[])
    assembled_prompt = '当前待确认 TaskPlan JSON: {"fight":{"stage":"SN-10"}} 用户新消息：换个活动关'
    with pytest.raises(PlannerValidationError):
        validate_planner_output(
            {"action": "patch", "patch": {"fight": {"stage": "SN-10"}}},
            snapshot,
            "换个活动关",
            "confirm",
        )
    validate_planner_output(
        {"action": "patch", "patch": {"fight": {"stage": "SN-10"}}},
        snapshot,
        assembled_prompt,
        "confirm",
    )


def test_validator_rejects_hallucinated_stage_that_only_looks_valid(tmp_path):
    snapshot = _snapshot(tmp_path, stages=[])
    with pytest.raises(PlannerValidationError):
        validate_planner_output(
            {"action": "run", "fight": {"enable": True, "stage": "SN-10"}},
            snapshot,
            "刷当前活动代币",
            "fresh",
        )


def test_advise_refs_must_exist_in_snapshot_and_reply_is_rendered_from_snapshot(tmp_path):
    snapshot = _snapshot(tmp_path, stages=[StageInfo("测试当期", "TT-8", "酮凝集", "x")])
    data = {"action": "advise", "advise_refs": ["TT-8", "CE-6"]}
    validate_planner_output(data, snapshot, "当前活动能刷什么", "fresh")
    reply = render_advise(data, snapshot)
    assert "TT-8" in reply and "酮凝集" in reply and "CE-6" in reply

    with pytest.raises(PlannerValidationError):
        validate_planner_output(
            {"action": "advise", "advise_refs": ["SN-10"]},
            snapshot,
            "当前活动能刷什么",
            "fresh",
        )


def test_schema_requires_patch_and_advise_refs_payloads():
    with pytest.raises(ValidationError):
        validate({"action": "patch"}, SCHEMA)
    with pytest.raises(ValidationError):
        validate({"action": "advise"}, SCHEMA)
