"""游戏机上跑的 copilot spike 集（S1/S4/S4b/S5）。headless 机跑不了，需模拟器+游戏。

用法（在项目根，先激活 .venv）：
  # S4：OperBox 识别经 maa-cli —— 拿识别结果落点（stdout? asst.log?）
  .venv/Scripts/python scripts/spike_copilot.py operbox

  # S1/S5：maa copilot 子命令 —— 验证自动编队+开打，且 --batch 下不卡交互
  #   先手动把游戏点到当期活动地图界面，再跑：
  .venv/Scripts/python scripts/spike_copilot.py copilot-sub <作业id>

  # S4b：验证生产路径 `maa run` 接受 Copilot 任务并能和 StartUp 组链
  #   从主界面开始，脚本会 StartUp→Copilot；作业 JSON 自动从 prts 下载落盘。
  .venv/Scripts/python scripts/spike_copilot.py copilot-run <作业id> [关卡显示号如 FC-EX-2]

  # S2：copilot_list（战斗列表）模式，验证能否从主界面自动导航到关卡再开打
  #   从主界面开始即可，stage_name 传关卡内部名（如 1-7 = main_01-07）
  .venv/Scripts/python scripts/spike_copilot.py copilot-list <作业id> [stage_name]

跑完把 stdout 存进 tests/fixtures/（沿用 capture_maa_output.py 模式），用于锁定解析规则。
"""

import json
import os
import subprocess
import sys
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，emoji/部分字符会 UnicodeEncodeError；强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from maa_remote.config import load_config
from maa_remote.executor import ensure_emulator


def _maa_env(cfg):
    env = dict(os.environ)
    env["MAA_CONFIG_DIR"] = cfg.maa.config_dir
    if cfg.maa.core_dir:
        env["MAA_CORE_DIR"] = cfg.maa.core_dir
    if cfg.maa.resource_dir:
        env["MAA_RESOURCE_DIR"] = cfg.maa.resource_dir
    adb_dir = os.path.dirname(cfg.emulator.adb_path)
    if adb_dir:
        env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")
    return env


def _run_maa_tasks(cfg, tasks, name):
    """写自定义 tasks.json 并 `maa run`，实时打印 stdout。返回退出码。"""
    task_dir = os.path.join(cfg.maa.config_dir, "tasks")
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, ensure_ascii=False, indent=2)
    print(f"[spike] tasks.json -> {path}\n{json.dumps(tasks, ensure_ascii=False, indent=2)}\n")

    cmd = [cfg.maa.maa_cli_path, "run", name, "-a", cfg.emulator.adb_serial, "--batch"]
    print(f"[spike] $ {' '.join(cmd)}\n" + "=" * 60)
    proc = subprocess.run(cmd, env=_maa_env(cfg), text=True)
    print("=" * 60 + f"\n[spike] exit={proc.returncode}")
    print(f"[spike] asst.log 尾部可看识别/结算细节: {cfg.maa.asst_log_path}")
    return proc.returncode


def _download_copilot(work_id):
    """单作业下载：/copilot/get/{id} → 落盘内层 content JSON，返回本地路径+stage_name。"""
    url = f"https://prts.maa.plus/copilot/get/{work_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "maa-remote-spike"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    content = resp.get("data", {}).get("content")
    doc = json.loads(content) if isinstance(content, str) else content
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime", "copilot")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{work_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"[spike] 作业 {work_id} 落盘 -> {path}  (stage_name={doc.get('stage_name')!r})")
    return path, doc.get("stage_name") or ""


def cmd_operbox(cfg, args):
    ensure_emulator(cfg)
    _run_maa_tasks(
        cfg,
        [
            {"type": "StartUp", "params": {"client_type": cfg.maa.client, "start_game_enabled": True}},
            {"type": "OperBox", "params": {}},
        ],
        f"spike_operbox_{uuid.uuid4().hex[:8]}",
    )
    print("[spike] >> 找 OperBox 识别结果：先看上面 stdout，没有就看 asst.log 里的 all_oper/own_opers 回调。")


def cmd_copilot_sub(cfg, args):
    if not args:
        sys.exit("需要作业 id：copilot-sub <id>")
    ensure_emulator(cfg)
    cmd = [
        cfg.maa.maa_cli_path, "copilot", f"maa://{args[0]}",
        "-a", cfg.emulator.adb_serial, "--formation", "--batch",
    ]
    print(f"[spike] $ {' '.join(cmd)}\n[spike] 观察：是否自动编队+开打；--batch 下是否卡在 'set up your formation' 交互。\n" + "=" * 60)
    proc = subprocess.run(cmd, env=_maa_env(cfg), text=True)
    print("=" * 60 + f"\n[spike] exit={proc.returncode}")


def cmd_copilot_run(cfg, args):
    if not args:
        sys.exit("需要作业 id：copilot-run <id> [显示关卡号]")
    path, stage_name = _download_copilot(args[0])
    ensure_emulator(cfg)
    _run_maa_tasks(
        cfg,
        [
            {"type": "StartUp", "params": {"client_type": cfg.maa.client, "start_game_enabled": True}},
            {
                "type": "Copilot",
                "params": {
                    "filename": path,
                    "formation": True,
                    "use_sanity_potion": False,
                },
            },
        ],
        f"spike_copilotrun_{uuid.uuid4().hex[:8]}",
    )
    print("[spike] >> 关键验证：`maa run` 是否接受 Copilot 任务类型 + 从 StartUp 主界面能否走到该关开打。")
    print("[spike]    若报未知任务类型 → 生产路径需改用 `maa copilot` 子命令，StartUp 组链方案作废，需重议 §五。")


def cmd_copilot_list(cfg, args):
    """S2：copilot_list（战斗列表）模式，验证能否从主界面自动导航到关卡再开打。"""
    if not args:
        sys.exit("需要作业 id：copilot-list <id> [stage_name]")
    path, stage_name = _download_copilot(args[0])
    if len(args) > 1:
        stage_name = args[1]
    if not stage_name:
        sys.exit("content 里 stage_name 为空，请显式传入：copilot-list <id> <stage_name(如 main_01-07)>")
    ensure_emulator(cfg)
    _run_maa_tasks(
        cfg,
        [
            {"type": "StartUp", "params": {"client_type": cfg.maa.client, "start_game_enabled": True}},
            {
                "type": "Copilot",
                "params": {
                    "copilot_list": [
                        {"filename": path, "stage_name": stage_name, "is_raid": False}
                    ],
                    "formation": True,
                    "use_sanity_potion": False,
                },
            },
        ],
        f"spike_copilotlist_{uuid.uuid4().hex[:8]}",
    )
    print(f"[spike] >> 关键验证(S2)：copilot_list 能否从【主界面】自动导航到 {stage_name} 并开打。")
    print("[spike]    到达编队→开打 = 主界面→关卡导航 MAA 内建已解决，S2 白解决；")
    print("[spike]    仍停在主界面/找不到关卡 = 需自写导航 override（§五方案2）。")


COMMANDS = {
    "operbox": cmd_operbox,
    "copilot-sub": cmd_copilot_sub,
    "copilot-run": cmd_copilot_run,
    "copilot-list": cmd_copilot_list,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"用法: python scripts/spike_copilot.py <{'|'.join(COMMANDS)}> [args]")
    cfg = load_config("config.toml")
    COMMANDS[sys.argv[1]](cfg, sys.argv[2:])


if __name__ == "__main__":
    main()
