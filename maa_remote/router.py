from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError, validate

from maa_remote.config import Config
from maa_remote.models import Fight, Msg, RouteResult, TaskPlan
from maa_remote.preview import plan_preview
from maa_remote.stage_catalog import format_menu, load_open_stages, resolve_selection


FAST_PATH = {"跑日常", "日常", "daily", "跑一下日常", "托管", "托管一下"}
CONFIRM_WORDS = {"确认", "确定", "是", "yes", "y", "1", "开始"}
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
        self._pending_selection: dict[str, tuple[list[Any], float]] = {}
        self._pending_confirm: dict[str, tuple[TaskPlan, float]] = {}

    def route(self, msg: Msg) -> RouteResult:
        pending_confirm = self._pending_confirm.get(msg.chat_id)
        if pending_confirm and self.now_fn() < pending_confirm[1]:
            handled = self._handle_confirm(msg, pending_confirm[0])
            if handled is not None:
                return handled
            self._pending_confirm.pop(msg.chat_id, None)
        elif pending_confirm:
            self._pending_confirm.pop(msg.chat_id, None)

        pending_selection = self._pending_selection.get(msg.chat_id)
        if pending_selection and self.now_fn() < pending_selection[1]:
            return self._handle_selection(msg, pending_selection[0])
        if pending_selection:
            self._pending_selection.pop(msg.chat_id, None)

        return self._route_fresh(msg)

    def _route_fresh(self, msg: Msg) -> RouteResult:
        text = msg.text.strip()
        if text in FAST_PATH:
            plan = TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks)
            return self._maybe_confirm(msg, plan)
        if text.startswith(SKIP_CONFIRM_PREFIX) and text[len(SKIP_CONFIRM_PREFIX) :].strip() in FAST_PATH:
            plan = TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks)
            return self._maybe_confirm(msg, plan, skip_confirm=True)

        plan_data = self._llm_plan(msg.text)
        if plan_data is None:
            return RouteResult(kind="reply", reply="没太懂，你是想跑日常还是刷某个具体关卡？")

        action = plan_data.get("action")
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

    def _maybe_confirm(self, msg: Msg, plan: TaskPlan, skip_confirm: bool = False) -> RouteResult:
        fight = plan.fight
        spend = fight.enable and (fight.stone > 0 or fight.medicine > 0)
        need_confirm = spend or (self.cfg.confirm.mode == "always" and not skip_confirm)
        if not need_confirm:
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)

        text = plan_preview(plan, self.cfg)
        if spend:
            text = "⚠️ 本计划包含花费（碎石/动用囤药），请核对后再确认！\n" + text
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
        return None

    def _handle_selection(self, msg: Msg, stages: list[Any]) -> RouteResult:
        code = resolve_selection(msg.text, stages)
        if code == "__cancel__":
            self._pending_selection.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply="好的，已取消。")
        if code is None:
            return RouteResult(kind="reply", reply="没听懂，回复编号或关卡号，或回复「取消」。")

        self._pending_selection.pop(msg.chat_id, None)
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
            ),
            note=f"刷活动关卡 {code}",
        )
        plan.recruit.enable = False
        plan.infrast.enable = False
        plan.mall.enable = False
        plan.award.enable = False
        return self._maybe_confirm(msg, plan)

    def _start_selection(self, msg: Msg) -> RouteResult:
        if self.hot_update_fn is not None:
            try:
                self.hot_update_fn(self.cfg.maa.maa_cli_path)
            except Exception:
                pass

        stages = self.stage_loader(self.cfg.maa.stage_activity_json, self.cfg.maa.client)
        if not stages:
            return RouteResult(kind="reply", reply="当前没有开放的活动关卡，要不要刷常规关/当前关？")

        self._pending_selection[msg.chat_id] = (
            stages,
            self.now_fn() + self.cfg.runtime.selection_ttl_s,
        )
        return RouteResult(kind="reply", reply=format_menu(stages))

    def _llm_plan(self, text: str) -> dict[str, Any] | None:
        user_prompt = text
        for _ in range(self.cfg.llm.max_retries + 1):
            try:
                raw = self.llm.chat(self.system_prompt, user_prompt, json_mode=True)
                plan_data = json.loads(raw)
                validate(plan_data, self.schema)
                return plan_data
            except (json.JSONDecodeError, ValidationError) as exc:
                user_prompt = (
                    f"{text}\n\n上次输出无法解析或不符合 schema（{exc.__class__.__name__}）。"
                    "只输出符合 schema 的 JSON。"
                )
            except Exception:
                user_prompt = f"{text}\n\n上次调用失败。只输出符合 schema 的 JSON。"
        return None
