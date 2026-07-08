from __future__ import annotations

import json
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid

from maa_remote.config import Config
from maa_remote.models import ExecResult, TaskPlan
from maa_remote.procutil import run_utf8
from maa_remote.progress import ProgressEvent, parse_progress_line

log = logging.getLogger("maa_remote.executor")


class EmulatorError(Exception):
    pass


class EmulatorNotReadyError(Exception):
    pass


class EmulatorStatus:
    def __init__(self, state: str, detail: str = ""):
        self.state = state
        self.detail = detail

    @property
    def is_running(self) -> bool:
        return self.state == "running"


_EXPIRING_MEDICINE_ALL = 999
_INFRAST_ONE_KEY_ROTATION_MODE = 20000


def build_task_file(plan: TaskPlan, client: str) -> dict:
    tasks: list[dict] = []
    if plan.startup:
        tasks.append(
            {
                "type": "StartUp",
                "params": {"client_type": client, "start_game_enabled": True},
            }
        )
    if plan.recruit.enable:
        tasks.append(
            {
                "type": "Recruit",
                "params": {
                    "refresh": True,
                    "select": [4, 5, 6],
                    "confirm": [3, 4, 5, 6],
                    "times": plan.recruit.max_times,
                    "set_time": True,
                    "expedite": False,
                },
            }
        )
    if plan.infrast.enable:
        tasks.append(
            {
                "type": "Infrast",
                "params": {
                    "mode": _INFRAST_ONE_KEY_ROTATION_MODE,
                    "facility": [
                        "Mfg",
                        "Trade",
                        "Power",
                        "Control",
                        "Reception",
                        "Office",
                        "Dorm",
                    ],
                    "drones": "_NotUse",
                },
            }
        )
    if plan.mall.enable:
        tasks.append(
            {
                "type": "Mall",
                "params": {
                    "shopping": True,
                    "buy_first": ["招聘许可", "龙门币"],
                    "blacklist": ["碳", "家具"],
                },
            }
        )
    if plan.award.enable:
        tasks.append({"type": "Award", "params": {"award": True}})
    if plan.fight.enable:
        fight_params = {
            "stage": plan.fight.stage,
            "expiring_medicine": (
                _EXPIRING_MEDICINE_ALL if plan.fight.expiring_medicine else 0
            ),
            "medicine": plan.fight.medicine,
            "stone": plan.fight.stone,
        }
        if plan.fight.times is not None:
            fight_params["times"] = plan.fight.times
        if plan.fight.series is not None:
            fight_params["series"] = plan.fight.series
        tasks.append({"type": "Fight", "params": fight_params})
    if plan.copilot.enable:
        # 最后一道边界：enable 但空 jobs 是坏计划，绝不静默跳过成"只启动游戏"。
        # for_copilot 已挡一层，这里防 router/patch/手写计划绕过 constructor。
        if not plan.copilot.jobs:
            raise ValueError("copilot plan enabled but has no jobs")
        tasks.extend(_nav_tasks(plan.copilot))
        tasks.append(_copilot_task(plan.copilot))
    return {"tasks": tasks}


def _nav_tasks(copilot) -> list[dict]:
    """主页→活动地图导航（S2）的 seam。

    ⚠️ 当前返回 []：S2「主页→终端→活动地图」override 尚未实现（实测 Copilot/copilot_list
    都不含这段，会卡在主页，见设计文档 §十一）。所以现在 StartUp 之后直接接 Copilot，
    **依赖游戏已停在编队/地图界面**——这是 pre-S2 的已知现状，不是假装能导航。
    S2 落地后在此返回导航任务序列（对起始态无假设）。
    """
    return []


def _copilot_task(copilot) -> dict:
    """按 §2.2 集成协议出 Copilot 任务。

    单 job 且非突袭 → filename 模式（S4b 实测过的路径）；
    多 job 或突袭 → copilot_list（每项带 stage_name/is_raid，地图内自动切关）。
    """
    params = {
        "formation": copilot.formation,
        "formation_index": copilot.formation_index,
        "use_sanity_potion": copilot.use_sanity_potion,
    }
    jobs = copilot.jobs
    if len(jobs) == 1 and not jobs[0].is_raid:
        params["filename"] = jobs[0].filename
    else:
        params["copilot_list"] = [
            {"filename": j.filename, "stage_name": j.stage_name, "is_raid": j.is_raid}
            for j in jobs
        ]
    return {"type": "Copilot", "params": params}


def ensure_emulator(
    cfg: Config,
    runner=run_utf8,
    sleep=time.sleep,
    monotonic=time.monotonic,
    on_event=None,
) -> None:
    emulator = cfg.emulator
    if on_event is not None:
        try:
            on_event(ProgressEvent("start", "🖥️ 拉起模拟器中…"))
        except Exception:
            log.exception("进度回调失败(忽略)")
    runner(shlex.split(emulator.launch_cmd), timeout=60)
    deadline = monotonic() + emulator.boot_timeout_s

    while monotonic() < deadline:
        runner([emulator.adb_path, "connect", emulator.adb_serial], timeout=15)
        state = runner(
            [emulator.adb_path, "-s", emulator.adb_serial, "get-state"],
            timeout=15,
        )
        if (getattr(state, "stdout", "") or "").strip() == "device":
            if on_event is not None:
                try:
                    on_event(ProgressEvent("done", "✅ 模拟器就绪"))
                except Exception:
                    log.exception("进度回调失败(忽略)")
            return
        sleep(2)

    raise EmulatorError(f"模拟器/adb 在 {emulator.boot_timeout_s}s 内未就绪（{emulator.adb_serial}）")


def emulator_status(cfg: Config, runner=run_utf8) -> EmulatorStatus:
    emulator = cfg.emulator
    try:
        runner([emulator.adb_path, "connect", emulator.adb_serial], timeout=15)
        state = runner(
            [emulator.adb_path, "-s", emulator.adb_serial, "get-state"],
            timeout=15,
        )
    except Exception as exc:
        return EmulatorStatus("unknown", str(exc))

    raw = (getattr(state, "stdout", "") or "").strip()
    if raw == "device":
        return EmulatorStatus("running", raw)
    if raw in {"offline", "unknown", "unauthorized"}:
        return EmulatorStatus("offline", raw)
    return EmulatorStatus("unknown", raw)


def ensure_emulator_running(cfg: Config, runner=run_utf8) -> None:
    status = emulator_status(cfg, runner=runner)
    if not status.is_running:
        detail = f": {status.detail}" if status.detail else ""
        raise EmulatorNotReadyError(f"模拟器未就绪（{status.state}{detail}）")


def shutdown_emulator(cfg: Config, runner=run_utf8) -> None:
    runner(shlex.split(cfg.emulator.shutdown_cmd), timeout=60)


def parse_maa_log(text: str) -> dict:
    facts: dict = {}
    lines = [line for line in text.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        if "summary" in line.lower():
            facts["summary"] = "\n".join(lines[index : index + 40])
            break

    recruit = re.search(r"公招[^\d]*(\d+)\s*次", text)
    if recruit:
        facts["recruit_times"] = int(recruit.group(1))

    fight = re.search(r"Fight\s+(\S+)[^\d]*(\d+)", text)
    if fight:
        facts["fight"] = f"{fight.group(1)} x{fight.group(2)}"

    if "换班完成" in text or "Infrast" in text:
        facts["infrast"] = "已换班"

    facts["raw_tail"] = "\n".join(lines[-15:])
    return facts


class AsstLogTailer:
    """maa 运行期间 tail MaaCore asst.log，把新增行解析成进度事件。"""

    def __init__(self, log_path: str, on_event, poll_interval_s: float = 1.0):
        self.log_path = log_path
        self.on_event = on_event
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def __enter__(self) -> "AsstLogTailer":
        try:
            self._offset = os.path.getsize(self.log_path)
        except OSError:
            self._offset = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain()
            self._stop.wait(self.poll_interval_s)
        self._drain()

    def _drain(self) -> None:
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return
        if size < self._offset:
            self._offset = 0

        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                for line in f:
                    event = parse_progress_line(line)
                    if event is not None:
                        try:
                            self.on_event(event)
                        except Exception:
                            log.exception("进度回调失败(忽略)")
                self._offset = f.tell()
        except OSError:
            pass


def run_maa(
    plan: TaskPlan,
    cfg: Config,
    task_dir: str,
    popen=subprocess.Popen,
    on_event=None,
    cancel_event: threading.Event | None = None,
) -> ExecResult:
    os.makedirs(task_dir, exist_ok=True)
    name = f"maa_remote_{uuid.uuid4().hex[:8]}"
    task_path = os.path.join(task_dir, f"{name}.json")
    with open(task_path, "w", encoding="utf-8") as f:
        json.dump(build_task_file(plan, cfg.maa.client), f, ensure_ascii=False, indent=2)

    cmd = [cfg.maa.maa_cli_path, "run", name, "-a", cfg.emulator.adb_serial, "--batch"]
    env = dict(os.environ)
    env["MAA_CONFIG_DIR"] = os.path.dirname(task_dir)
    if cfg.maa.core_dir:
        env["MAA_CORE_DIR"] = cfg.maa.core_dir
    if cfg.maa.resource_dir:
        env["MAA_RESOURCE_DIR"] = cfg.maa.resource_dir
    adb_dir = os.path.dirname(cfg.emulator.adb_path)
    if adb_dir:
        env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")

    try:
        proc = popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as exc:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=f"maa 启动失败: {exc}")

    timed_out = threading.Event()
    cancelled = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(cfg.maa.task_timeout_s, _kill)
    watchdog.daemon = True
    watchdog.start()

    use_tailer = bool(cfg.maa.asst_log_path) and on_event is not None
    lines: list[str] = []
    delivered = {"n": 0}

    def _emit(event: ProgressEvent) -> None:
        delivered["n"] += 1
        try:
            on_event(event)
        except Exception:
            log.exception("进度回调失败(忽略)")

    def _stop_proc() -> None:
        try:
            proc.terminate()
        except AttributeError:
            try:
                proc.kill()
            except Exception:
                pass
            return
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            return

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass

    def _reader(out_queue: queue.Queue[str | None]) -> None:
        try:
            for line in proc.stdout or []:
                out_queue.put(line)
        finally:
            out_queue.put(None)

    def _pump() -> None:
        out_queue: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=_reader, args=(out_queue,), daemon=True)
        reader.start()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled.set()
                _stop_proc()
                break
            try:
                line = out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            lines.append(line.rstrip("\n"))
            if on_event is not None and not use_tailer:
                event = parse_progress_line(line)
                if event is not None:
                    _emit(event)

    try:
        if use_tailer:
            with AsstLogTailer(cfg.maa.asst_log_path, _emit):
                _pump()
                returncode = proc.wait()
        else:
            _pump()
            returncode = proc.wait()
    finally:
        watchdog.cancel()

    if on_event is not None and delivered["n"] == 0 and not timed_out.is_set():
        _emit(ProgressEvent("info", "ℹ️ 本次没拿到细粒度进度，请等最终总结"))

    raw_log = "\n".join(lines)
    facts = parse_maa_log(raw_log)
    if cancelled.is_set():
        return ExecResult(
            ok=False,
            exit_code=-1,
            raw_log=raw_log,
            facts=facts,
            error="用户已停止本次 MAA 任务",
            cancelled=True,
        )
    if timed_out.is_set():
        return ExecResult(
            ok=False,
            exit_code=-1,
            raw_log=raw_log,
            facts=facts,
            error=f"MAA 超时(超过 {cfg.maa.task_timeout_s}s)，已强制终止",
        )
    if returncode != 0:
        return ExecResult(
            ok=False,
            exit_code=returncode,
            raw_log=raw_log,
            facts=facts,
            error=f"MAA 非零退出（退出码 {returncode}）",
        )
    return ExecResult(ok=True, exit_code=0, raw_log=raw_log, facts=facts, error=None)


def execute(
    plan: TaskPlan,
    cfg: Config,
    task_dir: str,
    runner=run_utf8,
    sleep=time.sleep,
    monotonic=time.monotonic,
    on_event=None,
    popen=subprocess.Popen,
    cancel_event: threading.Event | None = None,
    ensure_ready: bool = True,
) -> ExecResult:
    if ensure_ready:
        try:
            ensure_emulator_running(cfg, runner=runner)
        except EmulatorNotReadyError as exc:
            return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=str(exc))

    result = run_maa(
        plan,
        cfg,
        task_dir,
        popen=popen,
        on_event=on_event,
        cancel_event=cancel_event,
    )
    if cfg.emulator.close_after and not result.cancelled:
        shutdown_emulator(cfg, runner=runner)
    return result
