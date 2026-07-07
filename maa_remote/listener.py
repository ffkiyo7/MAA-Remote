from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Iterator

from maa_remote.config import Config
from maa_remote.models import Msg
from maa_remote.procutil import lark_profile_args, lark_subprocess_env, resolve_executable


log = logging.getLogger(__name__)


def _extract_flat_sender_open_id(sender_id) -> str:
    if isinstance(sender_id, dict):
        return sender_id.get("open_id") or sender_id.get("openId") or ""
    return str(sender_id or "")


def _extract_flat_text(content) -> str:
    if isinstance(content, dict):
        return str(content.get("text", "")).strip()
    text = str(content or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict):
            return str(parsed.get("text", "")).strip()
    return text


def parse_event(
    obj: dict,
    allowed_sender: str,
    max_age_s: int = 0,
    now_ms: int | None = None,
) -> Msg | None:
    event_type = obj.get("type") or obj.get("header", {}).get("event_type")
    if event_type != "im.message.receive_v1":
        return None

    if "event" in obj:
        event = obj.get("event", {})
        message = event.get("message", {})
        sender_open_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
        content_raw = message.get("content", "{}")
        try:
            content = json.loads(content_raw)
        except json.JSONDecodeError:
            return None
        text = str(content.get("text", "")).strip()
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        message_type = message.get("message_type")
        create_time_raw = message.get("create_time", "0")
    else:
        sender_open_id = _extract_flat_sender_open_id(obj.get("sender_id", ""))
        text = _extract_flat_text(obj.get("content", ""))
        message_id = obj.get("message_id") or obj.get("id", "")
        chat_id = obj.get("chat_id", "")
        message_type = obj.get("message_type")
        create_time_raw = obj.get("create_time", "0")

    if message_type != "text":
        return None

    if sender_open_id != allowed_sender:
        return None

    try:
        create_time = int(create_time_raw)
    except (TypeError, ValueError):
        create_time = 0

    if max_age_s > 0 and create_time > 0:
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if current_ms - create_time > max_age_s * 1000:
            log.info(
                "忽略过旧消息 message_id=%s（超过 %ss）",
                message_id,
                max_age_s,
            )
            return None
    elif max_age_s > 0:
        log.warning("消息 create_time 无法解析，跳过新鲜度过滤 message_id=%s", message_id)

    if not text:
        return None

    return Msg(
        text=text,
        chat_id=chat_id,
        message_id=message_id,
        sender_open_id=sender_open_id,
        create_time=create_time,
    )


def listen(
    cfg: Config,
    allowed_sender: str,
    max_age_s: int = 0,
    stop_event: threading.Event | None = None,
    spawn=subprocess.Popen,
    sleep=time.sleep,
) -> Iterator[Msg]:
    identity = "bot" if cfg.lark.identity in ("auto", "bot") else cfg.lark.identity
    cmd = [
        resolve_executable("lark-cli"),
        "event",
        "consume",
        cfg.lark.event_key,
        "--as",
        identity,
        "--quiet",
        *lark_profile_args(cfg.lark.profile),
    ]
    backoff_s = 1

    while stop_event is None or not stop_event.is_set():
        try:
            proc = spawn(
                cmd,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=lark_subprocess_env(),
            )
            if stop_event is not None:
                threading.Thread(
                    target=_terminate_on_stop,
                    args=(proc, stop_event),
                    daemon=True,
                ).start()
            for raw_line in proc.stdout:
                if stop_event is not None and stop_event.is_set():
                    break
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

        if stop_event is not None and stop_event.is_set():
            break
        log.warning("lark-cli event consume 退出，%s 秒后重启", backoff_s)
        sleep(backoff_s)
        backoff_s = min(backoff_s * 2, 60)


def _terminate_on_stop(proc, stop_event: threading.Event) -> None:
    stop_event.wait()
    try:
        proc.terminate()
    except AttributeError:
        try:
            proc.kill()
        except Exception:
            pass
    except Exception:
        pass
