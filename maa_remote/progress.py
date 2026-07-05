from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from maa_remote.procutil import run_utf8
from maa_remote.reporter import send_reply

log = logging.getLogger("maa_remote.progress")


@dataclass
class ProgressEvent:
    phase: str
    text: str


TASKCHAIN_LABELS: dict[str, tuple[str, str]] = {
    "StartUp": ("🎮", "启动游戏"),
    "Recruit": ("🎫", "公招"),
    "Infrast": ("🏗️", "基建换班"),
    "Mall": ("🛒", "信用商店"),
    "Award": ("🎁", "领奖励"),
    "Fight": ("⚔️", "刷理智"),
    "CloseDown": ("🚪", "关闭游戏"),
}

_CHAIN_RE = re.compile(r'"taskchain"\s*:\s*"(\w+)"')


def _label(chain: str) -> tuple[str, str]:
    return TASKCHAIN_LABELS.get(chain, ("▶️", chain))


def parse_progress_line(line: str) -> ProgressEvent | None:
    if not line or "SubTask" in line:
        return None
    m = _CHAIN_RE.search(line)
    if m:
        emoji, label = _label(m.group(1))
        if "TaskChainStart" in line:
            return ProgressEvent("start", f"{emoji} {label}中…")
        if "TaskChainCompleted" in line:
            return ProgressEvent("done", f"✅ {label}完成")
        if "TaskChainError" in line:
            return ProgressEvent("error", f"❌ {label}失败")
        if "TaskChainStopped" in line:
            return ProgressEvent("info", f"⏹️ {label}中止")
    return None


class ProgressSender:
    """把进度事件合并成简洁的飞书消息。任何异常只记日志，绝不影响执行。"""

    def __init__(
        self,
        anchor_message_id: str | None,
        trigger_message_id: str,
        identity: str,
        style: str = "thread",
        runner=run_utf8,
    ):
        self.anchor = anchor_message_id
        self.trigger = trigger_message_id
        self.identity = identity
        self.style = style if anchor_message_id else "flat"
        self.runner = runner
        self._pending_done: str | None = None

    def handle(self, event: ProgressEvent) -> None:
        try:
            if event.phase == "start":
                text = f"{self._pending_done} → {event.text}" if self._pending_done else event.text
                self._pending_done = None
                self._send(text)
            elif event.phase == "done":
                if self._pending_done:
                    self._send(self._pending_done)
                self._pending_done = event.text
            elif event.phase == "error":
                self.flush()
                self._send(event.text)
            else:
                self._send(event.text)
        except Exception:
            log.exception("进度推送失败(不影响执行)")

    def flush(self) -> None:
        try:
            if self._pending_done:
                self._send(self._pending_done)
                self._pending_done = None
        except Exception:
            log.exception("进度冲刷失败(不影响执行)")

    def _send(self, text: str) -> None:
        if self.style == "thread":
            send_reply(self.anchor, text, self.identity, runner=self.runner, reply_in_thread=True)
        else:
            send_reply(self.trigger, text, self.identity, runner=self.runner)
