from __future__ import annotations

import json
import os
import time
import copy
from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from typing import Any, Optional

from jsonschema import ValidationError, validate

from maa_remote.config import Config
from maa_remote.copilot_catalog import (
    CatalogResult,
    CopilotFetchError,
    StageResolutionError,
    build_candidates,
    load_stage_catalog,
)
from maa_remote.copilot_jobs import persist_from_result, resolve_jobs_dir
from maa_remote.models import Fight, Msg, RouteResult, TaskPlan
from maa_remote.planner_snapshot import (
    PlannerValidationError,
    build_planner_snapshot,
    build_user_prompt,
    render_advise,
    validate_planner_output,
)
from maa_remote.preview import plan_preview
from maa_remote.roster import Roster
from maa_remote.stage_catalog import format_menu, load_open_stages, resolve_selection


FAST_PATH = {"跑日常", "日常", "daily", "跑一下日常", "托管", "托管一下"}
CONFIRM_WORDS = {"确认", "确定", "是", "yes", "y", "1", "开始", "可以", "好", "行", "没问题"}
CANCEL_WORDS = {"取消", "算了", "不", "no", "n"}
SKIP_WORDS = {"跳过", "skip", "跳"}
SKIP_CONFIRM_PREFIX = "直接"


@dataclass
class CopilotSession:
    """抄作业待确认会话状态（§六）。confirm=事前候选确认；failure=失败后决策。"""

    phase: str                       # "confirm" | "failure"
    stage_display: str
    result: CatalogResult            # 候选 + contents（落盘用）
    selectable: list                 # 供用户编号选择的候选（已排序）
    formation_index: int = 0
    # failure 阶段专用（由 worker/#6 注入，#5 不锁定这些怎么产生）：
    remaining_stages: list = field(default_factory=list)
    detail: str = ""                 # 失败详情文案（"打到 2:31 暴毙，耗理智 15"）


def _candidate_status_icon(c) -> str:
    return "✅" if not c.risky else "⚠️"


def _fmt_top_candidate(c) -> list[str]:
    title = f"「{c.title}」" if c.title else ""
    lines = [f"作业 #{c.id}{title}（评级 {c.rating_level}★）"]
    if c.opers:
        lines.append("  编队：" + " · ".join(c.opers[:6]))
    if c.risks:
        lines.append("  ⚠️ " + "；".join(c.risks[:3]))
    return lines


def build_confirm_message(session: CopilotSession) -> str:
    """§六① 事前确认：列首选 + 其余候选编号。"""
    sel = session.selectable
    top_lines = _fmt_top_candidate(sel[0])
    lines = [f"📋 {session.stage_display} 计划用 {top_lines[0]}"]
    lines.extend(top_lines[1:])
    if len(sel) > 1:
        alts = "  ".join(
            f"{i}. #{c.id}{('「'+c.title+'」') if c.title else ''}{_candidate_status_icon(c)}"
            for i, c in enumerate(sel[1:], 2)
        )
        lines.append("其余候选：" + alts)
    lines.append("回「1」开打；回其它编号换候选；回「取消」放弃。")
    return "\n".join(lines)


def build_failure_message(session: CopilotSession) -> str:
    """§六② 失败后决策：报告 → 列换作业候选 / 跳过 / 取消。"""
    lines = [f"❌ {session.stage_display} 抄作业失败"]
    if session.detail:
        lines[0] += f"（{session.detail}）"
    if session.remaining_stages:
        lines.append("后面还剩：" + "、".join(session.remaining_stages))
    lines.append("怎么办：")
    for i, c in enumerate(session.selectable, 1):
        title = f"「{c.title}」" if c.title else ""
        lines.append(f"  {i}. 换作业 #{c.id}{title} 重打 {session.stage_display}")
    lines.append("  回「跳过」——不打这关；自动续跑还没接入，后续需单独再叫我")
    lines.append("  回「取消」——收工")
    return "\n".join(lines)


class Router:
    def __init__(
        self,
        cfg: Config,
        llm,
        system_prompt: str,
        schema: dict[str, Any],
        now_fn: Callable[[], float] = time.time,
        stage_loader: Callable = load_open_stages,
        hot_update_fn: Callable | None = None,
        copilot_catalog_fn: Callable | None = None,
        roster_provider: Callable[[], Roster] | None = None,
    ):
        self.cfg = cfg
        self.llm = llm
        self.system_prompt = system_prompt
        self.schema = schema
        self.now_fn = now_fn
        self.stage_loader = stage_loader
        self.hot_update_fn = hot_update_fn
        # catalog / roster 走 seam：默认真实实现（load stage_catalog + prts + roster.json 缓存），
        # 单测注入假实现，避免依赖网络/真实练度（roster #10 未定案前也能跑）。
        self.copilot_catalog_fn = copilot_catalog_fn or _make_catalog_fn(cfg)
        self.roster_provider = roster_provider or _make_roster_provider(cfg)
        self._pending_selection: dict[str, tuple[list[Any], float, TaskPlan | None]] = {}
        self._pending_confirm: dict[str, tuple[TaskPlan, float]] = {}
        self._pending_copilot: dict[str, tuple[CopilotSession, float]] = {}

    def route(self, msg: Msg) -> RouteResult:
        pending_copilot = self._pending_copilot.get(msg.chat_id)
        if pending_copilot and self.now_fn() < pending_copilot[1]:
            return self._handle_copilot_pending(msg, pending_copilot[0])
        if pending_copilot:
            self._pending_copilot.pop(msg.chat_id, None)

        pending_selection = self._pending_selection.get(msg.chat_id)
        if pending_selection and self.now_fn() < pending_selection[1]:
            return self._handle_selection(msg, pending_selection[0], pending_selection[2])
        if pending_selection:
            self._pending_selection.pop(msg.chat_id, None)

        pending_confirm = self._pending_confirm.get(msg.chat_id)
        if pending_confirm and self.now_fn() < pending_confirm[1]:
            handled = self._handle_confirm(msg, pending_confirm[0])
            if handled is not None:
                return handled
            self._pending_confirm.pop(msg.chat_id, None)
        elif pending_confirm:
            self._pending_confirm.pop(msg.chat_id, None)

        return self._route_fresh(msg)

    def _route_fresh(self, msg: Msg) -> RouteResult:
        text = msg.text.strip()
        if text in FAST_PATH:
            plan = TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks)
            return self._maybe_confirm(msg, plan)
        if text.startswith(SKIP_CONFIRM_PREFIX) and text[len(SKIP_CONFIRM_PREFIX) :].strip() in FAST_PATH:
            plan = TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks)
            return self._maybe_confirm(msg, plan, skip_confirm=True)

        snapshot = self._snapshot()
        plan_data = self._llm_plan(
            build_user_prompt(msg.text, snapshot),
            snapshot=snapshot,
            original_text=msg.text,
            mode="fresh",
        )
        if plan_data is None:
            return RouteResult(kind="reply", reply="没太懂，你是想跑日常还是刷某个具体关卡？")

        action = plan_data.get("action")
        if action in {"approve", "patch"}:
            return RouteResult(kind="reply", reply="当前没有待确认的计划。你是想跑日常，还是刷某个具体关卡？")
        if action == "advise":
            return RouteResult(kind="reply", reply=render_advise(plan_data, snapshot))
        if action == "ask_stage_selection":
            return self._start_selection(msg)
        if action == "copilot":
            return self._handle_copilot(msg, plan_data)
        if action == "clarify":
            return RouteResult(
                kind="reply",
                reply=plan_data.get("clarify_question") or "能说得再具体点吗？",
            )
        if action == "reject":
            return RouteResult(kind="reply", reply="这个我帮不上，我只负责跑明日方舟日常/刷关卡。")

        return self._maybe_confirm(msg, TaskPlan.from_llm_dict(plan_data, self.cfg.maa.fight))

    def _handle_copilot(self, msg: Msg, plan_data: dict[str, Any]) -> RouteResult:
        """抄作业入口：单关 → 跑 catalog → §六① 事前确认。

        绝不落到 from_llm_dict（否则会被当日常执行，spend_only 下直接跑掉 recruit/infrast 等）。
        批量(all_new)/未指定关先给诚实的"暂只支持单关"回复——不静默、不假装，留给后续接入。
        """
        copilot = plan_data.get("copilot") or {}
        scope = copilot.get("scope")
        stage = copilot.get("stage") or ""

        if scope == "single" and not stage:
            return RouteResult(kind="reply", reply="抄作业要打哪一关？告诉我关卡号（如 HS-9）。")
        if scope == "all_new":
            return RouteResult(
                kind="reply",
                reply="批量抄作业（整期新关）还在接入中，先告诉我具体一关（如 HS-9），我先把这关抄了。",
            )

        # 活动首日资源/新干员模板可能没更 → 先 hot-update（复用日常那套，失败不阻断）。
        if self.hot_update_fn is not None:
            try:
                self.hot_update_fn(self.cfg.maa.maa_cli_path)
            except Exception:
                pass

        roster = self.roster_provider()
        try:
            result = self.copilot_catalog_fn(stage, roster)
        except StageResolutionError:
            return RouteResult(kind="reply", reply=f"没找到关卡「{stage}」，是不是关卡号写错了？")
        except CopilotFetchError:
            return RouteResult(kind="reply", reply="查作业时网络出了点问题，等会儿再试试。")
        except OSError:
            # stage_catalog.json 还没建（首次使用/换机）→ 别崩，明确告知。
            return RouteResult(
                kind="reply",
                reply="作业关卡索引还没建好，需要先在游戏机上跑一次 build_stage_catalog 生成映射表。",
            )

        selectable = [c for c in result.candidates if c.passed]
        if not selectable:
            return RouteResult(
                kind="reply",
                reply=f"{stage} 暂时没有你能直接开起来的作业（缺人或练度不够），晚点作业多了再试。",
            )
        return RouteResult(kind="reply", reply=self._enter_copilot_confirm(msg.chat_id, stage, result, selectable))

    # --- 抄作业 pending 状态机（§六）------------------------------------------

    def _enter_copilot_confirm(
        self, chat_id: str, stage: str, result: CatalogResult, selectable: list
    ) -> str:
        session = CopilotSession(
            phase="confirm",
            stage_display=stage,
            result=result,
            selectable=selectable,
            formation_index=self.cfg.copilot.formation_index,
        )
        self._store_copilot_session(chat_id, session)
        return build_confirm_message(session)

    def start_failure_decision(
        self,
        chat_id: str,
        stage: str,
        result: CatalogResult,
        alternatives: list,
        remaining_stages: list | None = None,
        detail: str = "",
    ) -> str:
        """由 worker/#6 在抄作业失败后调用：登记失败决策 pending，返回 §六② 文案供其发送。

        入场时机、失败停在哪关、alternatives 怎么产生，都由调用方决定——本方法只管状态机。
        """
        session = CopilotSession(
            phase="failure",
            stage_display=stage,
            result=result,
            selectable=list(alternatives),
            formation_index=self.cfg.copilot.formation_index,
            remaining_stages=list(remaining_stages or []),
            detail=detail,
        )
        self._store_copilot_session(chat_id, session)
        return build_failure_message(session)

    def _store_copilot_session(self, chat_id: str, session: CopilotSession) -> None:
        self._pending_selection.pop(chat_id, None)
        self._pending_confirm.pop(chat_id, None)
        self._pending_copilot[chat_id] = (
            session,
            self.now_fn() + self.cfg.copilot.confirm_ttl_s,
        )

    def _handle_copilot_pending(self, msg: Msg, session: CopilotSession) -> RouteResult:
        text = msg.text.strip().lower()
        if text in CANCEL_WORDS:
            self._pending_copilot.pop(msg.chat_id, None)
            reply = "好的，收工。" if session.phase == "failure" else "好的，已取消。"
            return RouteResult(kind="reply", reply=reply)
        if session.phase == "failure" and text in SKIP_WORDS:
            self._pending_copilot.pop(msg.chat_id, None)
            # 诚实：批量续跑(#6)还没接入 → 跳过后不会自动接着打，别承诺"继续后面的"。
            if session.remaining_stages:
                rest = "、".join(session.remaining_stages)
                tail = f"。后面还有 {rest}，但自动续跑还没接入，要打的话回头单独叫我。"
            else:
                tail = "，没有后续了，收工。"
            return RouteResult(kind="reply", reply=f"好的，先跳过 {session.stage_display}{tail}")

        pick = self._resolve_pick(text, session.selectable)
        if pick is None:
            hint = "回编号换作业、回「跳过」或「取消」。" if session.phase == "failure" else "回「1」开打、回其它编号换候选、或回「取消」。"
            return RouteResult(kind="reply", reply=f"没听懂，{hint}")

        return self._execute_copilot_pick(msg, session, pick)

    def _resolve_pick(self, text: str, selectable: list):
        if not text.isdigit():
            return None
        idx = int(text) - 1
        if 0 <= idx < len(selectable):
            return selectable[idx]
        return None

    def _execute_copilot_pick(self, msg: Msg, session: CopilotSession, candidate) -> RouteResult:
        jobs_dir = resolve_jobs_dir(self.cfg)
        try:
            job = persist_from_result(session.result, candidate.id, jobs_dir)
        except (KeyError, OSError) as exc:
            self._pending_copilot.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply=f"作业落盘失败了，先不打这关：{exc}")
        plan = TaskPlan.for_copilot(
            [job],
            formation_index=session.formation_index,
            use_sanity_potion=False,
            note=f"抄作业 #{candidate.id} 打 {session.stage_display}",
        )
        self._pending_copilot.pop(msg.chat_id, None)
        return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)

    def _maybe_confirm(
        self,
        msg: Msg,
        plan: TaskPlan,
        skip_confirm: bool = False,
        force_confirm: bool = False,
    ) -> RouteResult:
        fight = plan.fight
        spend = fight.enable and (fight.stone > 0 or fight.medicine > 0)
        need_confirm = force_confirm or spend or (self.cfg.confirm.mode == "always" and not skip_confirm)
        if not need_confirm:
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)

        text = plan_preview(plan, self.cfg)
        if spend:
            text = "⚠️ 本计划包含花费（碎石/动用囤药），请核对后再确认！\n" + text
        self._pending_selection.pop(msg.chat_id, None)
        self._pending_confirm[msg.chat_id] = (
            plan,
            self.now_fn() + self.cfg.confirm.ttl_s,
        )
        return RouteResult(kind="reply", reply=text)

    def _handle_confirm(self, msg: Msg, plan: TaskPlan) -> RouteResult | None:
        text = msg.text.strip().lower()
        if text in CONFIRM_WORDS:
            self._pending_confirm.pop(msg.chat_id, None)
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)
        if text in CANCEL_WORDS:
            self._pending_confirm.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply="好的，已取消。")
        return self._handle_confirm_update(msg, plan)

    def _handle_confirm_update(self, msg: Msg, base_plan: TaskPlan) -> RouteResult:
        snapshot = self._snapshot(base_plan)
        plan_data = self._llm_plan(
            self._confirm_update_prompt(msg.text, base_plan, snapshot),
            snapshot=snapshot,
            original_text=msg.text,
            mode="confirm",
        )
        if plan_data is None:
            return RouteResult(
                kind="reply",
                reply="没太懂要怎么改。可以回复「确认」「取消」，或说清楚要改哪个关卡/子任务。",
            )

        action = plan_data.get("action")
        if action == "approve":
            self._pending_confirm.pop(msg.chat_id, None)
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=base_plan)
        if action == "ask_stage_selection":
            return self._start_selection(msg, base_plan=base_plan)
        if action == "advise":
            return RouteResult(kind="reply", reply=render_advise(plan_data, snapshot))
        if action == "clarify":
            return RouteResult(
                kind="reply",
                reply=plan_data.get("clarify_question") or "能说得再具体点吗？",
            )
        if action == "reject":
            return RouteResult(kind="reply", reply="这个改法不在 MAA 可执行的日常/刷关范围里。")
        if action != "patch":
            return RouteResult(
                kind="reply",
                reply="没太懂要怎么改。可以回复「确认」「取消」，或说清楚要改哪个关卡/子任务。",
            )

        updated = self._merge_plan_update(base_plan, plan_data["patch"])
        return self._maybe_confirm(msg, updated, force_confirm=True)

    def _confirm_update_prompt(
        self, text: str, base_plan: TaskPlan, snapshot: dict[str, Any]
    ) -> str:
        current = json.dumps(asdict(base_plan), ensure_ascii=False, sort_keys=True)
        return (
            "当前正在等待用户确认一份 TaskPlan。用户的新消息应优先理解为对这份待确认计划的补充或修改，"
            "而不是一个完全新任务。只改用户明确提到的字段，未提到的子任务和参数保持原计划。"
            "如果用户说“可以，但是...”，表示先按“但是”后的内容修改计划，不要直接执行。"
            "如果用户只是同意执行，输出 action=approve。"
            "如果用户要修改，输出 action=patch，patch 里只放用户明确提到的字段，禁止回显完整 TaskPlan。"
            "如果用户想换当前活动关但没给具体关卡，输出 action=ask_stage_selection。"
            "只输出符合 schema 的 JSON。\n"
            f"当前待确认 TaskPlan JSON:\n{current}\n\n"
            f"用户新消息：{text}\n\n"
            "结构化快照 snapshot：\n"
            f"{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}"
        )

    def _merge_plan_update(self, base_plan: TaskPlan, data: dict[str, Any]) -> TaskPlan:
        plan = copy.deepcopy(base_plan)
        plan.action = data.get("action", plan.action)
        plan.startup = True
        if "recruit" in data:
            recruit = data["recruit"]
            plan.recruit.enable = recruit.get("enable", plan.recruit.enable)
            plan.recruit.max_times = recruit.get("max_times", plan.recruit.max_times)
        for key in ("infrast", "mall", "award"):
            if key in data:
                toggle = getattr(plan, key)
                toggle.enable = data[key].get("enable", toggle.enable)
        if "fight" in data:
            fight = data["fight"]
            plan.fight.enable = fight.get("enable", plan.fight.enable)
            plan.fight.stage = fight.get("stage", plan.fight.stage)
            plan.fight.times = fight.get("times", plan.fight.times)
            plan.fight.expiring_medicine = fight.get(
                "expiring_medicine", plan.fight.expiring_medicine
            )
            plan.fight.medicine = fight.get("medicine", plan.fight.medicine)
            plan.fight.stone = fight.get("stone", plan.fight.stone)
            plan.fight.series = fight.get("series", plan.fight.series)
        plan.clarify_question = data.get("clarify_question", "")
        plan.note = data.get("note", plan.note)
        return plan

    def _handle_selection(
        self, msg: Msg, stages: list[Any], base_plan: TaskPlan | None = None
    ) -> RouteResult:
        code = resolve_selection(msg.text, stages)
        if code == "__cancel__":
            self._pending_selection.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply="好的，已取消。")
        if code is None:
            return RouteResult(kind="reply", reply="没听懂，回复编号或关卡号，或回复「取消」。")

        self._pending_selection.pop(msg.chat_id, None)
        if base_plan is not None:
            plan = copy.deepcopy(base_plan)
            plan.fight.enable = True
            plan.fight.stage = code
            plan.note = f"{plan.note}，刷理智关卡改为 {code}"
            return self._maybe_confirm(msg, plan, force_confirm=True)

        fight_defaults = self.cfg.maa.fight
        plan = TaskPlan(
            action="run",
            startup=True,
            fight=Fight(
                enable=True,
                stage=code,
                expiring_medicine=fight_defaults.expiring_medicine,
                medicine=fight_defaults.medicine,
                stone=fight_defaults.stone,
                series=fight_defaults.series,
            ),
            note=f"刷活动关卡 {code}",
        )
        plan.recruit.enable = False
        plan.infrast.enable = False
        plan.mall.enable = False
        plan.award.enable = False
        return self._maybe_confirm(msg, plan)

    def _start_selection(self, msg: Msg, base_plan: TaskPlan | None = None) -> RouteResult:
        if self.hot_update_fn is not None:
            try:
                self.hot_update_fn(self.cfg.maa.maa_cli_path)
            except Exception:
                pass

        stages = self.stage_loader(self.cfg.maa.stage_activity_json, self.cfg.maa.client)
        if not stages:
            return RouteResult(kind="reply", reply="当前没有开放的活动关卡，要不要刷常规关/当前关？")

        self._pending_confirm.pop(msg.chat_id, None)
        self._pending_selection[msg.chat_id] = (
            stages,
            self.now_fn() + self.cfg.runtime.selection_ttl_s,
            base_plan,
        )
        return RouteResult(kind="reply", reply=format_menu(stages))

    def _snapshot(self, pending_plan: TaskPlan | None = None) -> dict[str, Any]:
        return build_planner_snapshot(self.cfg, self.stage_loader, pending_plan)

    def _llm_plan(
        self,
        text: str,
        snapshot: dict[str, Any],
        original_text: str,
        mode: str,
    ) -> dict[str, Any] | None:
        user_prompt = text
        for _ in range(self.cfg.llm.max_retries + 1):
            try:
                raw = self.llm.chat(self.system_prompt, user_prompt, json_mode=True)
                plan_data = json.loads(raw)
                validate(plan_data, self.schema)
                validate_planner_output(plan_data, snapshot, original_text, mode)
                return plan_data
            except (json.JSONDecodeError, ValidationError, PlannerValidationError) as exc:
                detail = exc.message if isinstance(exc, ValidationError) else str(exc)
                user_prompt = (
                    f"{text}\n\n上次输出无法解析、不符合 schema 或未通过 planner validator"
                    f"（{exc.__class__.__name__}: {detail}）。"
                    "只输出符合 schema 的 JSON。"
                )
            except Exception:
                user_prompt = f"{text}\n\n上次调用失败。只输出符合 schema 的 JSON。"
        return None


def _make_catalog_fn(cfg: Config) -> Callable:
    """默认 catalog seam：加载 stage_catalog + 跑 prts 匹配管线。"""

    def run(stage_display: str, roster: Roster, level_id_override: Optional[str] = None) -> CatalogResult:
        cat_path = cfg.copilot.stage_catalog_json or os.path.join(
            resolve_jobs_dir(cfg), "stage_catalog.json"
        )
        catalog = load_stage_catalog(cat_path)
        return build_candidates(
            stage_display,
            roster,
            catalog=catalog,
            limit=cfg.copilot.candidates_limit,
            rating_min=cfg.copilot.rating_min,
            level_id_override=level_id_override,
        )

    return run


def _make_roster_provider(cfg: Config) -> Callable[[], Roster]:
    """默认 roster seam：读 <jobs_dir>/roster.json 缓存；缺失 → 空 Roster（降级：全标风险，不淘汰）。"""

    def provide() -> Roster:
        path = os.path.join(resolve_jobs_dir(cfg), "roster.json")
        try:
            return Roster.load(path)
        except OSError:
            return Roster()

    return provide
