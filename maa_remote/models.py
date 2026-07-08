from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from maa_remote.config import FightConfig


@dataclass
class Msg:
    text: str
    chat_id: str
    message_id: str
    sender_open_id: str
    create_time: int


@dataclass
class StageInfo:
    activity_name: str
    code: str
    drop: str
    expire_utc: str


@dataclass
class Fight:
    enable: bool = False
    stage: str = ""
    times: int | None = None
    expiring_medicine: bool = True
    medicine: int = 0
    stone: int = 0
    series: int | None = None


@dataclass
class Recruit:
    enable: bool = True
    max_times: int = 4


@dataclass
class Toggle:
    enable: bool = True


@dataclass
class CopilotJob:
    """一份落盘后的作业 + 它要打的关卡。

    保留 job_id/stage_display/level_id 作为执行后仍需的上下文：#5 确认文案、#6 失败报告
    「用的哪份作业、打哪一关、换候选/跳过哪一关」都要用到，别只留 filename 让下游反推。
    """

    filename: str            # 本地作业 JSON 路径（匹配阶段已有全文，落盘后引用本地，不二次下载）
    job_id: int = 0          # prts 作业 id（换候选/失败报告引用）
    stage_display: str = ""  # 显示号（如 HS-9），给用户看的
    level_id: str = ""       # 关卡内部 level_id（查询用）
    stage_name: str = ""     # copilot_list 地图内导航用；默认=level_id。单 filename 模式忽略此字段。
    # 注意：content.stage_name 实测可能为空（§十一 修正1），故由 catalog 的查询 level_id 兜底填充；
    # copilot_list 到底要 level_id / stage_id / 显示号，属 S2 导航未决，可由上层覆写。
    is_raid: bool = False    # 突袭


@dataclass
class Copilot:
    """Copilot（抄作业）执行规格。jobs 长度=1 可走单 filename，>1 走 copilot_list——由 executor 决定。"""

    enable: bool = False
    jobs: list[CopilotJob] = field(default_factory=list)
    formation: bool = True
    formation_index: int = 0
    use_sanity_potion: bool = False


@dataclass
class TaskPlan:
    action: str
    startup: bool = True
    recruit: Recruit = field(default_factory=Recruit)
    infrast: Toggle = field(default_factory=Toggle)
    mall: Toggle = field(default_factory=Toggle)
    award: Toggle = field(default_factory=Toggle)
    fight: Fight = field(default_factory=Fight)
    copilot: Copilot = field(default_factory=Copilot)
    clarify_question: str = ""
    note: str = ""

    @classmethod
    def from_llm_dict(cls, data: dict[str, Any], fight_defaults: FightConfig) -> "TaskPlan":
        recruit = data.get("recruit", {})
        fight = data.get("fight", {})
        return cls(
            action=data["action"],
            startup=True,
            recruit=Recruit(
                enable=recruit.get("enable", True),
                max_times=recruit.get("max_times", 4),
            ),
            infrast=Toggle(enable=data.get("infrast", {}).get("enable", True)),
            mall=Toggle(enable=data.get("mall", {}).get("enable", True)),
            award=Toggle(enable=data.get("award", {}).get("enable", True)),
            fight=Fight(
                enable=fight.get("enable", False),
                stage=fight.get("stage", fight_defaults.stage),
                times=fight.get("times"),
                expiring_medicine=fight.get(
                    "expiring_medicine", fight_defaults.expiring_medicine
                ),
                medicine=fight.get("medicine", fight_defaults.medicine),
                stone=fight.get("stone", fight_defaults.stone),
                series=fight.get("series", fight_defaults.series),
            ),
            clarify_question=data.get("clarify_question", ""),
            note=data.get("note", ""),
        )

    @classmethod
    def for_copilot(
        cls,
        jobs: list[CopilotJob],
        formation_index: int = 0,
        use_sanity_potion: bool = False,
        note: str = "",
    ) -> "TaskPlan":
        """抄作业执行计划：只跑 StartUp→[Nav]→Copilot，其余日常子任务全关。

        由 Router 在候选确认+落盘后构建（§六①）；LLM 不直接产出（它不知道本地作业路径）。
        空 jobs + enable=True 是语义上不可执行的计划 → 直接在模型层挡掉。
        """
        if not jobs:
            raise ValueError("copilot jobs must not be empty")
        return cls(
            action="run",
            startup=True,
            recruit=Recruit(enable=False),
            infrast=Toggle(enable=False),
            mall=Toggle(enable=False),
            award=Toggle(enable=False),
            fight=Fight(enable=False),
            copilot=Copilot(
                enable=True,
                jobs=list(jobs),
                formation=True,
                formation_index=formation_index,
                use_sanity_potion=use_sanity_potion,
            ),
            note=note,
        )

    @classmethod
    def daily(cls, fight_defaults: FightConfig, daily_tasks: list[str]) -> "TaskPlan":
        return cls(
            action="run",
            startup=True,
            recruit=Recruit(enable="recruit" in daily_tasks),
            infrast=Toggle(enable="infrast" in daily_tasks),
            mall=Toggle(enable="mall" in daily_tasks),
            award=Toggle(enable="award" in daily_tasks),
            fight=Fight(
                enable="fight" in daily_tasks,
                stage=fight_defaults.stage,
                expiring_medicine=fight_defaults.expiring_medicine,
                medicine=fight_defaults.medicine,
                stone=fight_defaults.stone,
                series=fight_defaults.series,
            ),
            note="跑日常",
        )


@dataclass
class ExecResult:
    ok: bool
    exit_code: int
    raw_log: str
    facts: dict[str, Any]
    error: str | None = None
    cancelled: bool = False


@dataclass
class RouteResult:
    kind: str
    reply: str | None = None
    plan: TaskPlan | None = None
