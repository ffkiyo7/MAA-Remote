from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from maa_remote.models import StageInfo
from maa_remote.procutil import run_utf8


def _parse_stage_time(value: str, tz_offset: int) -> datetime:
    parsed = datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    return parsed.replace(tzinfo=timezone(timedelta(hours=tz_offset)))


def load_open_stages(
    activity_json_path: str, client: str, now: datetime | None = None
) -> list[StageInfo]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    with Path(activity_json_path).open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)

    side_stories = data.get(client, {}).get("sideStoryStage", {})
    stages: list[StageInfo] = []
    for event in side_stories.values():
        activity = event.get("Activity", {})
        if "UtcStartTime" not in activity or "UtcExpireTime" not in activity:
            continue

        tz_offset = activity.get("TimeZone", 8)
        start = _parse_stage_time(activity["UtcStartTime"], tz_offset)
        end = _parse_stage_time(activity["UtcExpireTime"], tz_offset)
        if not start <= now <= end:
            continue

        activity_name = activity.get("StageName") or activity.get("Tip", "")
        for stage in event.get("Stages", []):
            code = stage.get("Value") or stage.get("Display")
            if not code:
                continue
            stages.append(
                StageInfo(
                    activity_name=activity_name,
                    code=code,
                    drop=stage.get("Drop", ""),
                    expire_utc=activity["UtcExpireTime"],
                )
            )
    return stages


def resolve_selection(text: str, stages: list[StageInfo]) -> str | None:
    normalized = text.strip()
    if normalized.lower() in {"取消", "cancel", "算了"}:
        return "__cancel__"

    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < len(stages):
            return stages[index].code
        return None

    for stage in stages:
        if normalized.lower() == stage.code.lower():
            return stage.code
    return None


def format_menu(stages: list[StageInfo]) -> str:
    activity_name = stages[0].activity_name if stages else ""
    lines = [f"当前活动「{activity_name}」可刷关卡："]
    for index, stage in enumerate(stages, 1):
        suffix = f"（{stage.drop}）" if stage.drop else ""
        lines.append(f"{index}. {stage.code}{suffix}")
    lines.append("回复编号或关卡号选择，回复「取消」放弃。")
    return "\n".join(lines)


def hot_update(maa_cli_path: str, runner=run_utf8) -> None:
    result = runner([maa_cli_path, "hot-update"], timeout=120)
    if getattr(result, "returncode", 0) != 0:
        stderr = getattr(result, "stderr", "")
        stdout = getattr(result, "stdout", "")
        detail = (stderr or stdout or f"exit code {result.returncode}").strip()
        raise RuntimeError(f"maa hot-update failed: {detail}")
