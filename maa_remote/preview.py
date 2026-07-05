from __future__ import annotations

from maa_remote.config import Config
from maa_remote.executor import build_task_file
from maa_remote.models import TaskPlan

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _num(i: int) -> str:
    return _CIRCLED[i - 1] if 1 <= i <= len(_CIRCLED) else f"{i}."


def _describe(task: dict) -> str:
    t = task["type"]
    p = task.get("params", {})
    if t == "StartUp":
        return "开游戏(已在游戏内则秒过)"
    if t == "Recruit":
        return f"公招:最多 {p.get('times', 4)} 次(自动选高星词条,不加急)"
    if t == "Infrast":
        return "基建:游戏内一键换班"
    if t == "Mall":
        buy = "、".join(p.get("buy_first", []))
        skip = "、".join(p.get("blacklist", []))
        return f"信用商店:优先买{buy}(不买{skip})"
    if t == "Award":
        return "领日常任务奖励 & 收邮件"
    if t == "Fight":
        stage = p.get("stage") or "上次/当前关卡"
        parts = [f"刷理智:{stage}"]
        if p.get("times") is not None:
            parts.append(f"最多 {p['times']} 次")
        parts.append("只吃快过期的药" if p.get("expiring_medicine") else "不吃过期药")
        medicine = p.get("medicine", 0)
        stone = p.get("stone", 0)
        parts.append(f"⚠️ 动用 {medicine} 瓶囤积理智药" if medicine > 0 else "不动囤药")
        parts.append(f"⚠️ 碎 {stone} 颗源石" if stone > 0 else "不碎石")
        return ",".join(parts)
    return t


def plan_preview(plan: TaskPlan, cfg: Config) -> str:
    tasks = build_task_file(plan, cfg.maa.client)["tasks"]
    lines = ["📋 本次计划"]
    for i, task in enumerate(tasks, 1):
        lines.append(f"{_num(i)} {_describe(task)}")
    ttl_min = max(1, cfg.confirm.ttl_s // 60)
    lines.append(f"回「1」或「确认」开始;回「取消」作废;{ttl_min} 分钟不回自动作废。")
    return "\n".join(lines)
