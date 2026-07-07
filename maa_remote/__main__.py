from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
import time
import unicodedata

from maa_remote.config import Config, load_config, resolve_allowed_sender
from maa_remote.executor import (
    emulator_status,
    ensure_emulator,
    execute as execute_task,
    shutdown_emulator,
)
from maa_remote.listener import listen
from maa_remote.llm import LLMClient
from maa_remote.procutil import lark_profile_args, resolve_executable, run_utf8
from maa_remote.progress import ProgressSender
from maa_remote.reporter import report, send_reply
from maa_remote.router import Router
from maa_remote.stage_catalog import hot_update


log = logging.getLogger("maa_remote")

STOP_WORDS = {
    "停下来",
    "停下",
    "停止",
    "别跑了",
    "不用跑了",
    "取消任务",
    "中止",
    "终止",
    "stop",
    "abort",
}
STOP_CONFIRM_WORDS = {"确认", "确定", "是", "yes", "y", "1", "开始", "可以", "好", "行", "没问题"}
STOP_CANCEL_WORDS = {"取消", "算了", "不", "no", "n"}
STOP_CONFIRM_REPLY = "确定要停止本次 MAA 任务吗？回复「确认」停止，回复「取消」继续。"
STOPPING_REPLY = "收到，正在停止当前 MAA 任务。"
KEEP_RUNNING_REPLY = "好的，继续执行当前任务。"
STOP_CONFIRM_TTL_S = 300
EMULATOR_STATUS_QUERIES = {"模拟器状态", "模拟器开着吗", "模拟器在吗", "emulatorstatus"}


class RuntimeState:
    def __init__(self, now_fn=time.time):
        self.now_fn = now_fn
        self._lock = threading.Lock()
        self.cancel_event: threading.Event | None = None
        self.pending_stop_until = 0.0

    def start_task(self, cancel_event: threading.Event) -> None:
        with self._lock:
            self.cancel_event = cancel_event
            self.pending_stop_until = 0.0

    def finish_task(self) -> None:
        with self._lock:
            self.cancel_event = None
            self.pending_stop_until = 0.0

    def is_running(self) -> bool:
        with self._lock:
            return self.cancel_event is not None

    def request_stop_confirm(self) -> None:
        with self._lock:
            self.pending_stop_until = self.now_fn() + STOP_CONFIRM_TTL_S

    def clear_stop_confirm(self) -> None:
        with self._lock:
            self.pending_stop_until = 0.0

    def has_pending_stop_confirm(self) -> bool:
        with self._lock:
            if not self.cancel_event:
                self.pending_stop_until = 0.0
                return False
            if self.pending_stop_until <= 0:
                return False
            if self.now_fn() <= self.pending_stop_until:
                return True
            self.pending_stop_until = 0.0
            return False

    def confirm_stop(self) -> bool:
        with self._lock:
            if not self.cancel_event:
                self.pending_stop_until = 0.0
                return False
            self.cancel_event.set()
            self.pending_stop_until = 0.0
            return True


def setup_logging(log_file: str) -> None:
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    console = logging.StreamHandler()
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _norm_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    return "".join(
        ch for ch in normalized if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def _is_stop_request(text: str) -> bool:
    normalized = _norm_text(text)
    return any(word in normalized for word in STOP_WORDS)


def _is_emulator_status_query(text: str) -> bool:
    return _norm_text(text) in EMULATOR_STATUS_QUERIES


def _format_emulator_status(cfg: Config, runner=run_utf8) -> str:
    status = emulator_status(cfg, runner=runner)
    if status.state == "running":
        return f"模拟器已开启，ADB 已连接：{cfg.emulator.adb_serial}。"
    if status.state == "offline":
        detail = f"（{status.detail}）" if status.detail else ""
        return f"模拟器未就绪{detail}，当前 ADB 目标：{cfg.emulator.adb_serial}。"
    detail = f"：{status.detail}" if status.detail else ""
    return f"暂时无法确认模拟器状态{detail}。"


def _handle_running_control(
    msg,
    cfg: Config,
    identity: str,
    runtime_state: RuntimeState,
    runner=run_utf8,
) -> bool:
    if not runtime_state.is_running():
        return False

    text = _norm_text(msg.text)
    profile = cfg.lark.profile
    if runtime_state.has_pending_stop_confirm():
        if text in STOP_CONFIRM_WORDS:
            if runtime_state.confirm_stop():
                send_reply(msg.message_id, STOPPING_REPLY, identity, runner=runner, profile=profile)
            else:
                send_reply(
                    msg.message_id,
                    "当前没有正在执行的任务。",
                    identity,
                    runner=runner,
                    profile=profile,
                )
            return True
        if text in STOP_CANCEL_WORDS:
            runtime_state.clear_stop_confirm()
            send_reply(msg.message_id, KEEP_RUNNING_REPLY, identity, runner=runner, profile=profile)
            return True
        if _is_stop_request(msg.text):
            send_reply(msg.message_id, STOP_CONFIRM_REPLY, identity, runner=runner, profile=profile)
            return True

    if _is_stop_request(msg.text):
        runtime_state.request_stop_confirm()
        send_reply(msg.message_id, STOP_CONFIRM_REPLY, identity, runner=runner, profile=profile)
        return True

    send_reply(msg.message_id, cfg.runtime.busy_reply, identity, runner=runner, profile=profile)
    return True


def handle_message(
    msg,
    router,
    cfg: Config,
    lock: threading.Lock,
    llm,
    identity: str,
    task_dir: str,
    runner=run_utf8,
    execute_fn=execute_task,
    thread_factory=threading.Thread,
    runtime_state: RuntimeState | None = None,
    service_stop_event: threading.Event | None = None,
) -> None:
    profile = cfg.lark.profile
    if _is_emulator_status_query(msg.text):
        reply = _format_emulator_status(cfg, runner=runner)
        send_reply(msg.message_id, reply, identity, runner=runner, profile=profile)
        return

    if runtime_state is not None and _handle_running_control(
        msg, cfg, identity, runtime_state, runner=runner
    ):
        return

    route_result = router.route(msg)
    log.info("消息路由: text=%r kind=%s identity=%s", msg.text, route_result.kind, identity)
    if route_result.kind == "reply":
        send_reply(msg.message_id, route_result.reply, identity, runner=runner, profile=profile)
        return

    if not lock.acquire(blocking=False):
        send_reply(msg.message_id, cfg.runtime.busy_reply, identity, runner=runner, profile=profile)
        return

    cancel_event = threading.Event()
    if runtime_state is not None:
        runtime_state.start_task(cancel_event)

    anchor_id = None
    if route_result.reply:
        anchor_id = send_reply(msg.message_id, route_result.reply, identity, runner=runner, profile=profile)

    sender = None
    if cfg.progress.enable:
        sender = ProgressSender(
            anchor_id,
            msg.message_id,
            identity,
            cfg.progress.style,
            runner=runner,
            profile=profile,
        )

    def _job() -> None:
        try:
            result = execute_fn(
                route_result.plan,
                cfg,
                task_dir,
                runner=runner,
                on_event=sender.handle if sender is not None else None,
                cancel_event=cancel_event,
            )
            if sender is not None:
                sender.flush()
            report(result, msg, llm, identity, runner=runner, profile=profile)
            if result.ok and service_stop_event is not None:
                service_stop_event.set()
        except Exception as exc:
            log.exception("worker 执行未捕获异常")
            send_reply(msg.message_id, f"执行崩了：{exc}", identity, runner=runner, profile=profile)
        finally:
            if runtime_state is not None:
                runtime_state.finish_task()
            lock.release()

    thread_factory(target=_job, daemon=True).start()


def _auth_status(profile: str = "") -> dict:
    result = run_utf8(
        [resolve_executable("lark-cli"), "auth", "status", *lark_profile_args(profile)],
        timeout=30,
    )
    return _parse_auth_status_output(result.stdout)


def _parse_auth_status_output(stdout: str) -> dict:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if data.get("userOpenId"):
        return data
    identities = data.get("identities") or {}
    user = identities.get("user") or {}
    user_open_id = user.get("openId")
    if user_open_id:
        return {**data, "userOpenId": user_open_id}
    return data


def main(config_path: str = "config.toml") -> None:
    cfg = load_config(config_path)
    setup_logging(cfg.runtime.log_file)
    allowed_sender = resolve_allowed_sender(cfg, lambda: _auth_status(cfg.lark.profile))
    identity = "bot" if cfg.lark.identity in ("auto", "bot") else cfg.lark.identity

    llm = LLMClient(
        cfg.llm.base_url,
        cfg.llm.api_key,
        cfg.llm.model,
        cfg.llm.request_timeout_s,
        thinking=cfg.llm.thinking,
        reasoning_effort=cfg.llm.reasoning_effort,
    )
    with open("schemas/task_plan.schema.json", encoding="utf-8") as f:
        schema = json.load(f)
    with open("prompts/router.system.md", encoding="utf-8") as f:
        system_prompt = f.read()

    router = Router(
        cfg,
        llm,
        system_prompt,
        schema,
        hot_update_fn=hot_update if cfg.maa.hot_update_before_catalog else None,
    )
    task_dir = os.path.join(cfg.maa.config_dir, "tasks")
    lock = threading.Lock()
    runtime_state = RuntimeState()
    service_stop_event = threading.Event()
    log.info("监听中，允许触发者 open_id=%s，任务目录=%s", allowed_sender, task_dir)

    try:
        ensure_emulator(cfg)
        for msg in listen(
            cfg,
            allowed_sender,
            max_age_s=cfg.runtime.max_msg_age_s,
            stop_event=service_stop_event,
        ):
            handle_message(
                msg,
                router,
                cfg,
                lock,
                llm,
                identity,
                task_dir,
                runtime_state=runtime_state,
                service_stop_event=service_stop_event,
            )
    finally:
        try:
            shutdown_emulator(cfg)
        except Exception:
            log.exception("服务退出时关闭模拟器失败")


if __name__ == "__main__":
    main()
