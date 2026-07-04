from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid

from maa_remote.config import Config
from maa_remote.models import ExecResult, TaskPlan
from maa_remote.procutil import run_utf8


class EmulatorError(Exception):
    pass


_EXPIRING_MEDICINE_ALL = 999


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
                    "mode": 0,
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
        tasks.append({"type": "Fight", "params": fight_params})
    return {"tasks": tasks}


def ensure_emulator(
    cfg: Config,
    runner=run_utf8,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> None:
    emulator = cfg.emulator
    runner(shlex.split(emulator.launch_cmd), timeout=60)
    deadline = monotonic() + emulator.boot_timeout_s

    while monotonic() < deadline:
        runner([emulator.adb_path, "connect", emulator.adb_serial], timeout=15)
        state = runner(
            [emulator.adb_path, "-s", emulator.adb_serial, "get-state"],
            timeout=15,
        )
        if (getattr(state, "stdout", "") or "").strip() == "device":
            return
        sleep(2)

    raise EmulatorError(f"模拟器/adb 在 {emulator.boot_timeout_s}s 内未就绪（{emulator.adb_serial}）")


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


def run_maa(plan: TaskPlan, cfg: Config, task_dir: str, runner=run_utf8) -> ExecResult:
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
        result = runner(cmd, env=env, timeout=cfg.maa.task_timeout_s)
    except Exception as exc:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=f"maa 启动失败: {exc}")

    raw_log = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    facts = parse_maa_log(raw_log)
    returncode = getattr(result, "returncode", 0)
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
) -> ExecResult:
    try:
        ensure_emulator(cfg, runner=runner, sleep=sleep, monotonic=monotonic)
    except EmulatorError as exc:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=str(exc))

    result = run_maa(plan, cfg, task_dir, runner=runner)
    if cfg.emulator.close_after:
        runner(shlex.split(cfg.emulator.shutdown_cmd), timeout=60)
    return result
