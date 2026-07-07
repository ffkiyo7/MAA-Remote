import json
import shutil

from maa_remote.config import load_config
from maa_remote.eval_router import run_case, subset_match


SCHEMA = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))


def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(
        str(tmp_path / "config.toml"),
        env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"},
    )


def test_subset_match_only_checks_present_fields():
    assert subset_match({"action": "run"}, {"action": "run", "startup": True})
    assert not subset_match({"action": "run"}, {"action": "reject"})


def test_subset_match_recurses_and_skips_note():
    expected = {"action": "run", "fight": {"enable": True, "stone": 0}, "note": "whatever"}
    actual = {
        "action": "run",
        "fight": {"enable": True, "stone": 0, "stage": ""},
        "note": "别的",
    }
    assert subset_match(expected, actual)
    actual_bad = {"action": "run", "fight": {"enable": True, "stone": 50}}
    assert not subset_match(expected, actual_bad)


def test_run_case_pass(tmp_path):
    class LLM:
        def chat(self, system, user, json_mode=False):
            assert "结构化快照 snapshot" in user
            assert "钱本" in user
            return json.dumps(
                {
                    "action": "run",
                    "startup": True,
                    "fight": {"enable": True, "stage": "ce6", "times": 3},
                }
            )

    ok, why = run_case(
        LLM(),
        "SYS",
        SCHEMA,
        {"input": "打 3 次钱本", "expected": {"action": "run", "fight": {"stage": "CE-6"}}},
        _cfg(tmp_path),
    )
    assert ok, why


def test_run_case_fails_on_schema_violation(tmp_path):
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps({"action": "no_such_action"})

    ok, why = run_case(
        LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}}, _cfg(tmp_path)
    )
    assert not ok and "schema" in why


def test_run_case_fails_on_bad_json(tmp_path):
    class LLM:
        def chat(self, system, user, json_mode=False):
            return "not json"

    ok, why = run_case(
        LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}}, _cfg(tmp_path)
    )
    assert not ok and "JSON" in why


def test_run_case_fails_on_subset_mismatch(tmp_path):
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps({"action": "reject"})

    ok, why = run_case(
        LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}}, _cfg(tmp_path)
    )
    assert not ok and "字段" in why


def test_run_case_fails_on_planner_validator(tmp_path):
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps({"action": "run", "fight": {"enable": True, "stage": "SN-10"}})

    ok, why = run_case(
        LLM(),
        "SYS",
        SCHEMA,
        {"input": "刷当前活动代币", "expected": {"action": "ask_stage_selection"}},
        _cfg(tmp_path),
    )
    assert not ok and "planner validator" in why
