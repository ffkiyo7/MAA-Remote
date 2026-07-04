import json
import os
import textwrap

import pytest

from maa_remote.config import load_config
from maa_remote.executor import (
    EmulatorError,
    build_task_file,
    ensure_emulator,
    execute,
    parse_maa_log,
    run_maa,
)
from maa_remote.models import Fight, Recruit, TaskPlan, Toggle


_CONFIG = textwrap.dedent(
    """
    [lark]
    allowed_sender_open_id = ""
    app_id = ""
    identity = "auto"
    event_key = "im.message.receive_v1"
    [llm]
    provider = "deepseek"
    model = "deepseek-chat"
    base_url = "https://api.deepseek.com"
    api_key_env = "DEEPSEEK_API_KEY"
    request_timeout_s = 30
    max_retries = 1
    cache_system_prompt = true
    [maa]
    maa_cli_path = "C:/maa/maa.exe"
    core_dir = "D:/MAA-GUI"
    resource_dir = "D:/MAA-GUI/resource"
    config_dir = "C:/loong/config"
    stage_activity_json = "C:/loong/cache/StageActivityV2.json"
    client = "Official"
    hot_update_before_catalog = true
    task_timeout_s = 3600
    daily_tasks = ["startup", "recruit", "infrast", "mall", "award", "fight"]
    [maa.fight]
    stage = ""
    expiring_medicine = true
    medicine = 0
    stone = 0
    [emulator]
    kind = "mumu"
    vmindex = 0
    launch_cmd = '"C:/Program Files/Mu Mu/MuMuManager.exe" control -v 0 launch'
    shutdown_cmd = '"C:/Program Files/Mu Mu/MuMuManager.exe" control -v 0 shutdown'
    adb_path = "C:/Program Files/Mu Mu/adb.exe"
    adb_serial = "127.0.0.1:16384"
    boot_timeout_s = 120
    close_after = false
    [runtime]
    busy_reply = "busy"
    ack_reply = "ack"
    selection_ttl_s = 300
    max_msg_age_s = 300
    log_file = "logs/maa_remote.log"
    """
)


def _cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG, encoding="utf-8")
    return load_config(str(path), env={"DEEPSEEK_API_KEY": "k"})


class R:
    def __init__(self, out="", code=0, err=""):
        self.stdout = out
        self.stderr = err
        self.returncode = code


def test_build_task_file_daily_includes_toggled_tasks():
    plan = TaskPlan(
        action="run",
        startup=True,
        recruit=Recruit(enable=True, max_times=4),
        infrast=Toggle(True),
        mall=Toggle(True),
        award=Toggle(True),
        fight=Fight(enable=True, stage="", expiring_medicine=True, medicine=0, stone=0),
    )
    task_file = build_task_file(plan, "Official")
    types = [task["type"] for task in task_file["tasks"]]
    assert types == ["StartUp", "Recruit", "Infrast", "Mall", "Award", "Fight"]
    fight = task_file["tasks"][-1]
    assert fight["params"]["stage"] == ""
    assert fight["params"]["expiring_medicine"] == 999
    assert fight["params"]["medicine"] == 0 and fight["params"]["stone"] == 0


def test_build_task_file_omits_disabled():
    plan = TaskPlan(
        action="run",
        startup=False,
        recruit=Recruit(enable=False),
        infrast=Toggle(False),
        mall=Toggle(False),
        award=Toggle(False),
        fight=Fight(enable=True, stage="TT-8", times=3),
    )
    task_file = build_task_file(plan, "Official")
    types = [task["type"] for task in task_file["tasks"]]
    assert types == ["Fight"]
    assert task_file["tasks"][0]["params"]["stage"] == "TT-8"
    assert task_file["tasks"][0]["params"]["times"] == 3


def test_build_task_file_explicit_medicine_and_stone():
    plan = TaskPlan(
        action="run",
        startup=False,
        recruit=Recruit(enable=False),
        infrast=Toggle(False),
        mall=Toggle(False),
        award=Toggle(False),
        fight=Fight(enable=True, stage="1-7", medicine=999, stone=50),
    )
    task_file = build_task_file(plan, "Official")
    assert task_file["tasks"][0]["params"]["medicine"] == 999
    assert task_file["tasks"][0]["params"]["stone"] == 50


def test_ensure_emulator_splits_quoted_spaced_path(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return R("device\n") if cmd[-1] == "get-state" else R()

    ensure_emulator(cfg, runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0)
    assert calls[0] == [
        "C:/Program Files/Mu Mu/MuMuManager.exe",
        "control",
        "-v",
        "0",
        "launch",
    ]


def test_ensure_emulator_retries_connect_each_poll(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []
    state = {"polls": 0}

    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "get-state":
            state["polls"] += 1
            return R("device\n" if state["polls"] >= 3 else "offline\n")
        return R()

    ensure_emulator(cfg, runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0)
    connects = [call for call in calls if len(call) >= 2 and call[1] == "connect"]
    assert len(connects) >= 3


def test_ensure_emulator_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    clock = {"v": 0.0}

    def mono():
        clock["v"] += 60.0
        return clock["v"]

    with pytest.raises(EmulatorError):
        ensure_emulator(cfg, runner=lambda cmd, **kw: R("offline\n"), sleep=lambda s: None, monotonic=mono)


def test_parse_maa_log_extracts_summary_section():
    log = "噪音行\n[INFO] Summary\nFight TT-8 3 times\n公招识别 4 次\n"
    facts = parse_maa_log(log)
    assert "Fight TT-8" in facts["summary"]
    assert facts["recruit_times"] == 4
    assert facts["fight"] == "TT-8 x3"
    assert facts["raw_tail"]


def test_run_maa_writes_task_and_injects_env(tmp_path):
    cfg = _cfg(tmp_path)
    task_dir = str(tmp_path / "tasks")
    plan = TaskPlan(action="run", fight=Fight(enable=True, stage="TT-8"))
    captured = {}

    def runner(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        captured["timeout"] = kw.get("timeout")
        return R("Summary\nFight TT-8 1 times\n")

    res = run_maa(plan, cfg, task_dir, runner=runner)
    assert res.ok is True and res.exit_code == 0
    assert captured["cmd"][0] == cfg.maa.maa_cli_path
    assert "run" in captured["cmd"] and "--batch" in captured["cmd"]
    assert "--no-summary" not in captured["cmd"]
    assert "-a" in captured["cmd"] and cfg.emulator.adb_serial in captured["cmd"]
    assert captured["env"]["MAA_CORE_DIR"] == "D:/MAA-GUI"
    assert captured["env"]["MAA_RESOURCE_DIR"] == "D:/MAA-GUI/resource"
    assert captured["env"]["MAA_CONFIG_DIR"] == os.path.dirname(task_dir)
    assert captured["env"]["PATH"].split(os.pathsep)[0] == "C:/Program Files/Mu Mu"
    assert captured["timeout"] == 3600
    assert any(name.endswith(".json") for name in os.listdir(task_dir))
    written = json.load(open(os.path.join(task_dir, os.listdir(task_dir)[0]), encoding="utf-8"))
    assert written["tasks"][-1]["type"] == "Fight"


def test_run_maa_nonzero_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), runner=lambda cmd, **kw: R("boom", code=2))
    assert res.ok is False and res.exit_code == 2 and res.error


def test_run_maa_runner_exception_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))

    def boom(cmd, **kw):
        raise TimeoutError("too slow")

    res = run_maa(plan, cfg, str(tmp_path / "tasks"), runner=boom)
    assert res.ok is False and "too slow" in res.error


def test_execute_emulator_failure_short_circuits(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    clock = {"v": 0.0}

    def mono():
        clock["v"] += 60.0
        return clock["v"]

    res = execute(
        plan,
        cfg,
        str(tmp_path / "tasks"),
        runner=lambda cmd, **kw: R("offline\n"),
        sleep=lambda s: None,
        monotonic=mono,
    )
    assert res.ok is False and "未就绪" in res.error


def test_execute_closes_emulator_when_configured(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.emulator.close_after = True
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "get-state":
            return R("device\n")
        if len(cmd) > 1 and cmd[1] == "run":
            return R("Summary\nFight TT-8 1 times\n")
        return R()

    res = execute(plan, cfg, str(tmp_path / "tasks"), runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0)
    assert res.ok is True
    assert calls[-1] == ["C:/Program Files/Mu Mu/MuMuManager.exe", "control", "-v", "0", "shutdown"]
