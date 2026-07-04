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


@dataclass
class Recruit:
    enable: bool = True
    max_times: int = 4


@dataclass
class Toggle:
    enable: bool = True


@dataclass
class TaskPlan:
    action: str
    startup: bool = True
    recruit: Recruit = field(default_factory=Recruit)
    infrast: Toggle = field(default_factory=Toggle)
    mall: Toggle = field(default_factory=Toggle)
    award: Toggle = field(default_factory=Toggle)
    fight: Fight = field(default_factory=Fight)
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
            ),
            clarify_question=data.get("clarify_question", ""),
            note=data.get("note", ""),
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


@dataclass
class RouteResult:
    kind: str
    reply: str | None = None
    plan: TaskPlan | None = None
