"""Intent routing regression runner.

Usage:
    python -m maa_remote.eval_router [--cases PATH] [--config PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from jsonschema import ValidationError, validate

from maa_remote.config import load_config
from maa_remote.llm import LLMClient, LLMError
from maa_remote.planner_snapshot import (
    PlannerValidationError,
    build_planner_snapshot,
    build_user_prompt,
    validate_planner_output,
)
from maa_remote.stage_catalog import load_open_stages


def subset_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            if key == "note":
                continue
            if key not in actual or not subset_match(value, actual[key]):
                return False
        return True
    return expected == actual


def run_case(
    llm,
    system_prompt: str,
    schema: dict,
    case: dict,
    cfg,
    stage_loader=load_open_stages,
) -> tuple[bool, str]:
    snapshot = build_planner_snapshot(cfg, stage_loader)
    user_prompt = build_user_prompt(case["input"], snapshot)
    try:
        raw = llm.chat(system_prompt, user_prompt, json_mode=True)
    except LLMError as exc:
        return False, f"LLM 调用失败: {exc}"
    except Exception as exc:
        return False, f"LLM 调用异常: {exc}"

    try:
        actual = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"非法 JSON: {raw[:120]}"

    try:
        validate(actual, schema)
    except ValidationError as exc:
        return False, f"schema 不过: {exc.message}"

    try:
        validate_planner_output(actual, snapshot, case["input"], mode="fresh")
    except PlannerValidationError as exc:
        return False, f"planner validator 不过: {exc}"

    if not subset_match(case["expected"], actual):
        return False, f"字段不匹配，实际: {json.dumps(actual, ensure_ascii=False)}"
    return True, ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="evals/router_cases.jsonl")
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.llm.api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        return 2

    llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model, cfg.llm.request_timeout_s)
    with open("schemas/task_plan.schema.json", encoding="utf-8") as f:
        schema = json.load(f)
    with open("prompts/router.system.md", encoding="utf-8") as f:
        system_prompt = f.read()
    with open(args.cases, encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]

    passed = 0
    for index, case in enumerate(cases, 1):
        ok, why = run_case(llm, system_prompt, schema, case, cfg)
        if ok:
            passed += 1
        mark = "PASS" if ok else "FAIL"
        suffix = "" if ok else f"\n       {why}"
        print(f"[{mark}] {index:02d} {case['input']}{suffix}")

    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
