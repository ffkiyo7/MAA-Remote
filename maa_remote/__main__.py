from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading

from maa_remote.config import Config, load_config, resolve_allowed_sender
from maa_remote.executor import execute as execute_task
from maa_remote.listener import listen
from maa_remote.llm import LLMClient
from maa_remote.procutil import resolve_executable, run_utf8
from maa_remote.progress import ProgressSender
from maa_remote.reporter import report, send_reply
from maa_remote.router import Router
from maa_remote.stage_catalog import hot_update


log = logging.getLogger("maa_remote")


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
) -> None:
    route_result = router.route(msg)
    log.info("消息路由: text=%r kind=%s identity=%s", msg.text, route_result.kind, identity)
    if route_result.kind == "reply":
        send_reply(msg.message_id, route_result.reply, identity, runner=runner)
        return

    if not lock.acquire(blocking=False):
        send_reply(msg.message_id, cfg.runtime.busy_reply, identity, runner=runner)
        return

    anchor_id = None
    if route_result.reply:
        anchor_id = send_reply(msg.message_id, route_result.reply, identity, runner=runner)

    sender = None
    if cfg.progress.enable:
        sender = ProgressSender(
            anchor_id,
            msg.message_id,
            identity,
            cfg.progress.style,
            runner=runner,
        )

    def _job() -> None:
        try:
            result = execute_fn(
                route_result.plan,
                cfg,
                task_dir,
                runner=runner,
                on_event=sender.handle if sender is not None else None,
            )
            if sender is not None:
                sender.flush()
            report(result, msg, llm, identity, runner=runner)
        except Exception as exc:
            log.exception("worker 执行未捕获异常")
            send_reply(msg.message_id, f"执行崩了：{exc}", identity, runner=runner)
        finally:
            lock.release()

    thread_factory(target=_job, daemon=True).start()


def _auth_status() -> dict:
    result = run_utf8([resolve_executable("lark-cli"), "auth", "status"], timeout=30)
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
    allowed_sender = resolve_allowed_sender(cfg, _auth_status)
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
    log.info("监听中，允许触发者 open_id=%s，任务目录=%s", allowed_sender, task_dir)

    for msg in listen(cfg, allowed_sender, max_age_s=cfg.runtime.max_msg_age_s):
        handle_message(msg, router, cfg, lock, llm, identity, task_dir)


if __name__ == "__main__":
    main()
