from __future__ import annotations

import json
import time
import copy
from dataclasses import asdict
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError, validate

from maa_remote.config import Config
from maa_remote.models import Fight, Msg, RouteResult, TaskPlan
from maa_remote.planner_snapshot import (
    PlannerValidationError,
    build_planner_snapshot,
    build_user_prompt,
    render_advise,
    validate_planner_output,
)
from maa_remote.preview import plan_preview
from maa_remote.stage_catalog import format_menu, load_open_stages, resolve_selection


FAST_PATH = {"跑日常", "日常", "daily", "跑一下日常", "托管", "托管一下"}
CONFIRM_WORDS = {"确认", "确定", "是", "yes", "y", "1", "开始", "可以", "好", "行", "没问题"}
CANCEL_WORDS = {"取消", "算了", "不", "no", "n"}
SKIP_CONFIRM_PREFIX = "直接"


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
    ):
        self.cfg = cfg
        self.llm = llm
        self.system_prompt = system_prompt
        self.schema = schema
        self.now_fn = now_fn
        self.stage_loader = stage_loader
        self.hot_update_fn = hot_update_fn
        self._pending_selection: dict[str, tuple[list[Any], float, TaskPlan | None]] = {}
        self._pending_confirm: dict[str, tuple[TaskPlan, float]] = {}

    def route(self, msg: Msg) -> RouteResult:
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
        if action == "clarify":
            return RouteResult(
                kind="reply",
                reply=plan_data.get("clarify_question") or "能说得再具体点吗？",
            )
        if action == "reject":
            return RouteResult(kind="reply", reply="这个我帮不上，我只负责跑明日方舟日常/刷关卡。")

        return self._maybe_confirm(msg, TaskPlan.from_llm_dict(plan_data, self.cfg.maa.fight))

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
