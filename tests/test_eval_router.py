import json

from maa_remote.eval_router import run_case, subset_match


SCHEMA = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))


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


def test_run_case_pass():
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps(
                {
                    "action": "run",
                    "startup": True,
                    "fight": {"enable": True, "stage": "CE-6", "times": 3},
                }
            )

    ok, why = run_case(
        LLM(),
        "SYS",
        SCHEMA,
        {"input": "打CE-6三次", "expected": {"action": "run", "fight": {"stage": "CE-6"}}},
    )
    assert ok, why


def test_run_case_fails_on_schema_violation():
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps({"action": "no_such_action"})

    ok, why = run_case(LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}})
    assert not ok and "schema" in why


def test_run_case_fails_on_bad_json():
    class LLM:
        def chat(self, system, user, json_mode=False):
            return "not json"

    ok, why = run_case(LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}})
    assert not ok and "JSON" in why


def test_run_case_fails_on_subset_mismatch():
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps({"action": "reject"})

    ok, why = run_case(LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}})
    assert not ok and "字段" in why
