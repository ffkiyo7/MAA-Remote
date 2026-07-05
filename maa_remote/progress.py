from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("maa_remote.progress")


@dataclass
class ProgressEvent:
    phase: str
    text: str


TASKCHAIN_LABELS: dict[str, tuple[str, str]] = {
    "StartUp": ("🎮", "启动游戏"),
    "Recruit": ("🎫", "公招"),
    "Infrast": ("🏗️", "基建换班"),
    "Mall": ("🛒", "信用商店"),
    "Award": ("🎁", "领奖励"),
    "Fight": ("⚔️", "刷理智"),
    "CloseDown": ("🚪", "关闭游戏"),
}

_CHAIN_RE = re.compile(r'"taskchain"\s*:\s*"(\w+)"')


def _label(chain: str) -> tuple[str, str]:
    return TASKCHAIN_LABELS.get(chain, ("▶️", chain))


def parse_progress_line(line: str) -> ProgressEvent | None:
    if not line or "SubTask" in line:
        return None
    m = _CHAIN_RE.search(line)
    if m:
        emoji, label = _label(m.group(1))
        if "TaskChainStart" in line:
            return ProgressEvent("start", f"{emoji} {label}中…")
        if "TaskChainCompleted" in line:
            return ProgressEvent("done", f"✅ {label}完成")
        if "TaskChainError" in line:
            return ProgressEvent("error", f"❌ {label}失败")
        if "TaskChainStopped" in line:
            return ProgressEvent("info", f"⏹️ {label}中止")
    return None
