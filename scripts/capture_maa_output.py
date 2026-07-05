"""实跑一次日常，抓 maa-cli 完整输出样本，用于锁定进度解析规则。

用法: .venv/Scripts/python scripts/capture_maa_output.py
可选: 先 set MAA_LOG=debug 再跑，获得更详细输出。
"""

import json
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from maa_remote.config import load_config
from maa_remote.executor import build_task_file, ensure_emulator
from maa_remote.models import TaskPlan


def main() -> None:
    cfg = load_config("config.toml")
    ensure_emulator(cfg)

    task_dir = os.path.join(cfg.maa.config_dir, "tasks")
    os.makedirs(task_dir, exist_ok=True)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    name = f"capture_{uuid.uuid4().hex[:8]}"
    with open(os.path.join(task_dir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(build_task_file(plan, cfg.maa.client), f, ensure_ascii=False, indent=2)

    env = dict(os.environ)
    env["MAA_CONFIG_DIR"] = os.path.dirname(task_dir)
    if cfg.maa.core_dir:
        env["MAA_CORE_DIR"] = cfg.maa.core_dir
    if cfg.maa.resource_dir:
        env["MAA_RESOURCE_DIR"] = cfg.maa.resource_dir
    adb_dir = os.path.dirname(cfg.emulator.adb_path)
    if adb_dir:
        env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")

    cmd = [cfg.maa.maa_cli_path, "run", name, "-a", cfg.emulator.adb_serial, "--batch"]
    out_path = "logs/maa_stdout_capture.txt"
    os.makedirs("logs", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in proc.stdout:
            out.write(line)
            out.flush()
            print(line, end="")
        code = proc.wait()
    print(f"\nexit={code}, saved to {out_path}")


if __name__ == "__main__":
    main()
