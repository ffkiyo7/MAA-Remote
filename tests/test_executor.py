import json
import os
import textwrap

import pytest

from conftest import FakePopen
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
        fight=Fight(enable=True, stage="", expiring_medicine=True, medicine=0, stone=0, series=0),
    )
    task_file = build_task_file(plan, "Official")
    types = [task["type"] for task in task_file["tasks"]]
    assert types == ["StartUp", "Recruit", "Infrast", "Mall", "Award", "Fight"]
    infrast = task_file["tasks"][2]
    assert infrast["params"]["mode"] == 20000
    fight = task_file["tasks"][-1]
    assert fight["params"]["stage"] == ""
    assert fight["params"]["expiring_medicine"] == 999
    assert fight["params"]["medicine"] == 0 and fight["params"]["stone"] == 0
    assert fight["params"]["series"] == 0


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


def test_build_task_file_allows_fixed_series():
    plan = TaskPlan(
        action="run",
        startup=False,
        recruit=Recruit(enable=False),
        infrast=Toggle(False),
        mall=Toggle(False),
        award=Toggle(False),
        fight=Fight(enable=True, stage="CE-6", series=6),
    )
    task_file = build_task_file(plan, "Official")
    assert task_file["tasks"][0]["params"]["series"] == 6


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
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    task_dir = str(tmp_path / "tasks")
    popen = FakePopen(["Summary", "all done"])
    res = run_maa(plan, cfg, task_dir, popen=popen)
    assert res.ok is True
    assert popen.cmd[0] == cfg.maa.maa_cli_path and "--batch" in popen.cmd
    env = popen.kw["env"]
    assert env["MAA_CONFIG_DIR"] == os.path.dirname(task_dir)
    assert env["MAA_CORE_DIR"] == cfg.maa.core_dir
    assert env["MAA_RESOURCE_DIR"] == cfg.maa.resource_dir
    assert env["PATH"].split(os.pathsep)[0] == "C:/Program Files/Mu Mu"
    assert popen.kw["encoding"] == "utf-8" and popen.kw["errors"] == "replace"
    assert "Summary" in res.raw_log
    assert any(name.endswith(".json") for name in os.listdir(task_dir))
    written = json.load(open(os.path.join(task_dir, os.listdir(task_dir)[0]), encoding="utf-8"))
    assert written["tasks"][0]["type"] == "StartUp"


def test_run_maa_nonzero_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(["boom"], returncode=2))
    assert res.ok is False and "退出码 2" in res.error


def test_run_maa_popen_exception_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen([], boom=OSError("no exe")))
    assert res.ok is False and "maa 启动失败" in res.error


ASST = 'Assistant::append_callback | TaskChain{kind} {{"taskchain":"{chain}","taskid":1,"uuid":"X"}}'


def test_run_maa_emits_progress_events_in_order(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    lines = [
        ASST.format(kind="Start", chain="StartUp"),
        "noise",
        ASST.format(kind="Completed", chain="StartUp"),
        ASST.format(kind="Start", chain="Recruit"),
        ASST.format(kind="Completed", chain="Recruit"),
    ]
    events = []
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(lines), on_event=events.append)
    assert res.ok is True
    assert [e.phase for e in events] == ["start", "done", "start", "done"]
    assert "公招" in events[2].text


def test_run_maa_on_event_exception_does_not_fail_run(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    lines = [ASST.format(kind="Start", chain="Recruit")]

    def bad_cb(event):
        raise RuntimeError("callback broke")

    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(lines), on_event=bad_cb)
    assert res.ok is True


def test_run_maa_timeout_kills_and_reports(tmp_path):
    import time as _t

    cfg = _cfg(tmp_path)
    cfg.maa.task_timeout_s = 0.05
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)

    class SlowPopen(FakePopen):
        @property
        def stdout(self):
            def gen():
                for line in self._lines:
                    if self.killed:
                        return
                    _t.sleep(0.03)
                    yield line + "\n"

            return gen()

    popen = SlowPopen(["line"] * 50)
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=popen)
    assert res.ok is False and "超时" in res.error
    assert popen.killed is True


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
        popen=FakePopen([""]),
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
        return R()

    res = execute(
        plan,
        cfg,
        str(tmp_path / "tasks"),
        runner=runner,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
        popen=FakePopen(["Summary", "Fight TT-8 1 times"]),
    )
    assert res.ok is True
    assert calls[-1] == ["C:/Program Files/Mu Mu/MuMuManager.exe", "control", "-v", "0", "shutdown"]


def test_ensure_emulator_emits_progress_events(tmp_path):
    cfg = _cfg(tmp_path)
    events = []

    def runner(cmd, **kw):
        return R("device\n")

    ensure_emulator(cfg, runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0, on_event=events.append)
    assert [e.phase for e in events] == ["start", "done"]
    assert "模拟器" in events[0].text and "模拟器" in events[1].text


def test_run_maa_no_signals_emits_fallback_notice(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    events = []
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(["no signal here"]), on_event=events.append)
    assert res.ok is True
    assert [e.phase for e in events] == ["info"]
    assert "细粒度进度" in events[0].text


def test_run_maa_with_asst_log_path_uses_tailer_not_stdout(tmp_path):
    cfg = _cfg(tmp_path)
    log_path = tmp_path / "asst.log"
    log_path.write_text("", encoding="utf-8")
    cfg.maa.asst_log_path = str(log_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    lines = [ASST.format(kind="Start", chain="Recruit")]
    events = []
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(lines), on_event=events.append)
    assert res.ok is True
    assert [e.phase for e in events] == ["info"]


def test_asst_log_tailer_reads_only_new_lines(tmp_path):
    import time as _t
    from maa_remote.executor import AsstLogTailer

    log_path = tmp_path / "asst.log"
    log_path.write_text(ASST.format(kind="Start", chain="StartUp") + "\n", encoding="utf-8")
    events = []
    with AsstLogTailer(str(log_path), events.append, poll_interval_s=0.01):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(ASST.format(kind="Start", chain="Recruit") + "\n")
        deadline = _t.monotonic() + 2
        while not events and _t.monotonic() < deadline:
            _t.sleep(0.02)
    assert len(events) == 1
    assert events[0].phase == "start" and "公招" in events[0].text


def test_asst_log_tailer_resets_offset_when_log_rotates(tmp_path):
    import time as _t
    from maa_remote.executor import AsstLogTailer

    log_path = tmp_path / "asst.log"
    log_path.write_text("old line\n" * 100, encoding="utf-8")
    events = []
    with AsstLogTailer(str(log_path), events.append, poll_interval_s=0.01):
        log_path.write_text(ASST.format(kind="Start", chain="Recruit") + "\n", encoding="utf-8")
        deadline = _t.monotonic() + 2
        while not events and _t.monotonic() < deadline:
            _t.sleep(0.02)
    assert len(events) == 1
    assert events[0].phase == "start" and "公招" in events[0].text
