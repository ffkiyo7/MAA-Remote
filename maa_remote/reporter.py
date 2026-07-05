from __future__ import annotations

import json
import logging

from maa_remote.models import ExecResult, Msg
from maa_remote.procutil import resolve_executable, run_utf8

log = logging.getLogger("maa_remote.reporter")

_POLISH_SYSTEM = (
    "你是明日方舟托管助手的汇报员。根据给定的执行事实，用简洁友好的中文口语总结这次跑的结果，"
    "带上关键数字（公招/刷关/基建等），有异常要点明。只输出总结文本。"
)


def _facts_template(result: ExecResult) -> str:
    lines = ["跑完了。" if result.ok else "跑的过程中出问题了。"]
    facts = result.facts or {}
    if "fight" in facts:
        lines.append(f"作战：{facts['fight']}")
    if "recruit_times" in facts:
        lines.append(f"公招：{facts['recruit_times']} 次")
    if "infrast" in facts:
        lines.append(f"基建：{facts['infrast']}")
    if result.error:
        lines.append(f"异常：{result.error}")
    if len(lines) == 1 and facts.get("raw_tail"):
        lines.append(f"日志尾部：{facts['raw_tail']}")
    return "\n".join(lines)


def build_summary(result: ExecResult, note: str, llm) -> str:
    fallback = _facts_template(result)
    if result.ok:
        user = f"用户意图：{note}\n执行事实：{result.facts}"
    else:
        user = f"用户意图：{note}\n执行失败，事实：{result.facts}\n错误：{result.error}"
    try:
        return llm.chat(_POLISH_SYSTEM, user)
    except Exception:
        return fallback


def _extract_message_id(stdout: str) -> str | None:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    message_id = data.get("message_id")
    if message_id:
        return message_id
    inner = data.get("data")
    if isinstance(inner, dict) and inner.get("message_id"):
        return inner["message_id"]
    return None


def send_reply(
    message_id: str,
    text: str,
    identity: str,
    runner=run_utf8,
    reply_in_thread: bool = False,
) -> str | None:
    cmd = [
        resolve_executable("lark-cli"),
        "im",
        "+messages-reply",
        "--message-id",
        message_id,
        "--text",
        text,
        "--as",
        identity,
        "--json",
    ]
    if reply_in_thread:
        cmd.append("--reply-in-thread")
    try:
        proc = runner(cmd, timeout=30)
    except Exception:
        log.exception("发送飞书回复失败")
        return None
    if getattr(proc, "returncode", 0) != 0:
        log.warning("发送飞书回复失败: %s", getattr(proc, "stderr", ""))
        return None
    return _extract_message_id(getattr(proc, "stdout", ""))


def report(result: ExecResult, msg: Msg, llm, identity: str, runner=run_utf8) -> None:
    summary = build_summary(result, msg.text, llm)
    send_reply(msg.message_id, summary, identity, runner=runner)
