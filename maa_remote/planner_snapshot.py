from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maa_remote.config import Config
from maa_remote.models import StageInfo, TaskPlan


STAGE_ALIASES: dict[str, str] = {
    "龙门币本": "CE-6",
    "钱本": "CE-6",
    "刷钱": "CE-6",
    "经验本": "LS-6",
    "狗粮": "LS-6",
    "作战记录": "LS-6",
    "红票本": "AP-5",
    "采购凭证": "AP-5",
    "技能书本": "CA-5",
    "技巧概要": "CA-5",
    "碳本": "SK-5",
}


class PlannerValidationError(ValueError):
    pass


def build_planner_snapshot(
    cfg: Config,
    stage_loader,
    pending_plan: TaskPlan | None = None,
) -> dict[str, Any]:
    stages = _load_activity_stages(cfg, stage_loader)
    return {
        "maa_capabilities": {
            "tasks": ["StartUp", "Fight", "Recruit", "Infrast", "Mall", "Award"],
            "fight_params": ["stage", "times", "series"],
            "fight_defaults": {
                "stage": cfg.maa.fight.stage,
                "expiring_medicine": cfg.maa.fight.expiring_medicine,
                "medicine": cfg.maa.fight.medicine,
                "stone": cfg.maa.fight.stone,
                "series": cfg.maa.fight.series,
            },
            "spend_requires_confirmation": ["stone", "medicine"],
        },
        "aliases": [
            {"name": name, "stage": stage} for name, stage in sorted(STAGE_ALIASES.items())
        ],
        "open_activity_stages": [asdict(stage) for stage in stages],
        "activity_cache": _activity_cache_info(cfg.maa.stage_activity_json),
        "pending_plan": asdict(pending_plan) if pending_plan is not None else None,
    }


def build_user_prompt(text: str, snapshot: dict[str, Any]) -> str:
    return (
        "用户原始消息：\n"
        f"{text}\n\n"
        "结构化快照 snapshot（只能基于这里的能力、别名、活动关卡和 pending_plan 规划）：\n"
        f"{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}"
    )


def validate_planner_output(
    plan_data: dict[str, Any],
    snapshot: dict[str, Any],
    original_text: str,
    mode: str,
) -> None:
    action = plan_data.get("action")
    if mode == "confirm" and action == "run":
        raise PlannerValidationError("confirmation mode requires action=patch or action=approve")
    if action == "run":
        _validate_stage_payload(plan_data, snapshot, original_text)
        return
    if action == "patch":
        if mode != "confirm":
            return
        _validate_stage_payload(plan_data.get("patch", {}), snapshot, original_text)
        return
    if action == "advise":
        _validate_advise_refs(plan_data, snapshot)


def render_advise(plan_data: dict[str, Any], snapshot: dict[str, Any]) -> str:
    refs = plan_data.get("advise_refs") or []
    cache_text = _format_cache_text(snapshot.get("activity_cache", {}))
    activity_by_code = {
        stage["code"]: stage for stage in snapshot.get("open_activity_stages", [])
    }
    alias_labels = _alias_labels(snapshot)

    if not refs:
        return f"本地活动缓存里没有可引用的开放活动关卡。数据时间：{cache_text}。"

    lines = [f"基于本地活动缓存（数据时间：{cache_text}），当前可参考："]
    for ref in refs:
        if ref in activity_by_code:
            stage = activity_by_code[ref]
            drop = f"：{stage['drop']}" if stage.get("drop") else ""
            activity = f"（{stage['activity_name']}）" if stage.get("activity_name") else ""
            lines.append(f"- {ref}{activity}{drop}")
        elif ref in alias_labels:
            lines.append(f"- {ref}：{alias_labels[ref]}")
        else:
            lines.append(f"- {ref}")
    return "\n".join(lines)


def normalize_stage_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).upper()
    return "".join(ch for ch in normalized if ch.isalnum())


def _load_activity_stages(cfg: Config, stage_loader) -> list[StageInfo]:
    try:
        return list(stage_loader(cfg.maa.stage_activity_json, cfg.maa.client))
    except Exception:
        return []


def _activity_cache_info(path: str) -> dict[str, Any]:
    info: dict[str, Any] = {"path": path, "mtime": None, "status": "missing"}
    try:
        stat = Path(path).stat()
    except OSError:
        return info

    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    info.update({"mtime": mtime, "status": "ok"})
    return info


def _validate_stage_payload(
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    original_text: str,
) -> None:
    fight = payload.get("fight")
    if not isinstance(fight, dict) or "stage" not in fight:
        return

    stage = fight.get("stage")
    if stage in (None, ""):
        return
    canonical_alias = _canonical_alias_stage(str(stage), snapshot)
    if canonical_alias is not None:
        fight["stage"] = canonical_alias
        return
    if _stage_appears_in_original_text(str(stage), original_text):
        return
    raise PlannerValidationError(
        f"fight.stage={stage!r} is not an alias stage and does not appear in the original user message"
    )


def _validate_advise_refs(plan_data: dict[str, Any], snapshot: dict[str, Any]) -> None:
    allowed = _advise_ref_codes(snapshot)
    missing = [ref for ref in plan_data.get("advise_refs", []) if ref not in allowed]
    if missing:
        raise PlannerValidationError(f"advise_refs contains unknown refs: {missing}")


def _canonical_alias_stage(stage: str, snapshot: dict[str, Any]) -> str | None:
    stage_key = normalize_stage_key(stage)
    for item in snapshot.get("aliases", []):
        if normalize_stage_key(item["stage"]) == stage_key:
            return item["stage"]
    return None


def _advise_ref_codes(snapshot: dict[str, Any]) -> set[str]:
    refs = {item["stage"] for item in snapshot.get("aliases", [])}
    refs.update(stage["code"] for stage in snapshot.get("open_activity_stages", []))
    return refs


def _stage_appears_in_original_text(stage: str, original_text: str) -> bool:
    stage_key = normalize_stage_key(stage)
    text_key = normalize_stage_key(original_text)
    return bool(stage_key) and stage_key in text_key


def _alias_labels(snapshot: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, list[str]] = {}
    for item in snapshot.get("aliases", []):
        labels.setdefault(item["stage"], []).append(item["name"])
    return {stage: " / ".join(names) for stage, names in labels.items()}


def _format_cache_text(cache: dict[str, Any]) -> str:
    if cache.get("mtime"):
        return str(cache["mtime"])
    if cache.get("path"):
        return f"未找到缓存文件 {cache['path']}"
    return "未知"
