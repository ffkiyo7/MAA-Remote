from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Iterator

from maa_remote.config import Config
from maa_remote.models import Msg


log = logging.getLogger(__name__)


def parse_event(
    obj: dict,
    allowed_sender: str,
    max_age_s: int = 0,
    now_ms: int | None = None,
) -> Msg | None:
    if obj.get("header", {}).get("event_type") != "im.message.receive_v1":
        return None

    event = obj.get("event", {})
    message = event.get("message", {})
    if message.get("message_type") != "text":
        return None

    sender_open_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    if sender_open_id != allowed_sender:
        return None

    try:
        create_time = int(message.get("create_time", "0"))
    except (TypeError, ValueError):
        create_time = 0

    if max_age_s > 0:
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if current_ms - create_time > max_age_s * 1000:
            log.info(
                "忽略过旧消息 message_id=%s（超过 %ss）",
                message.get("message_id"),
                max_age_s,
            )
            return None

    try:
        content = json.loads(message.get("content", "{}"))
    except json.JSONDecodeError:
        return None

    text = str(content.get("text", "")).strip()
    if not text:
        return None

    return Msg(
        text=text,
        chat_id=message.get("chat_id", ""),
        message_id=message.get("message_id", ""),
        sender_open_id=sender_open_id,
        create_time=create_time,
    )


def listen(
    cfg: Config,
    allowed_sender: str,
    max_age_s: int = 0,
    spawn=subprocess.Popen,
    sleep=time.sleep,
) -> Iterator[Msg]:
    identity = "bot" if cfg.lark.identity in ("auto", "bot") else cfg.lark.identity
    cmd = [
        "lark-cli",
        "event",
        "consume",
        cfg.lark.event_key,
        "--as",
        identity,
        "--quiet",
    ]
    backoff_s = 1

    while True:
        try:
            proc = spawn(
                cmd,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = parse_event(obj, allowed_sender, max_age_s=max_age_s)
                if msg is None:
                    continue

                backoff_s = 1
                yield msg
        except Exception:
            log.exception("listener 子进程异常")

        log.warning("lark-cli event consume 退出，%s 秒后重启", backoff_s)
        sleep(backoff_s)
        backoff_s = min(backoff_s * 2, 60)
