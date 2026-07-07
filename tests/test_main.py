import threading
import shutil

from maa_remote.__main__ import RuntimeState, _parse_auth_status_output, handle_message, setup_logging
from maa_remote.config import load_config
from maa_remote.models import ExecResult, Fight, Msg, RouteResult, TaskPlan


def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(
        str(tmp_path / "config.toml"),
        env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"},
    )


class FakeRouter:
    def __init__(self, rr):
        self.rr = rr

    def route(self, msg):
        return self.rr


class RaisingRouter:
    def route(self, msg):
        raise AssertionError("router should not be called")


class OKLLM:
    def chat(self, system, user, json_mode=False):
        return "done"


class ImmediateThread:
    def __init__(self, target, daemon=None):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


def _msg():
    return Msg(
        text="跑日常",
        chat_id="oc_1",
        message_id="om_1",
        sender_open_id="ou_1",
        create_time=0,
    )


def _runner_recording(sent):
    def runner(cmd, **kw):
        sent.append(cmd)

        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return R()

    return runner


def test_setup_logging_creates_log_directory(tmp_path):
    setup_logging(str(tmp_path / "logs" / "maa_remote.log"))
    assert (tmp_path / "logs").is_dir()


def test_parse_auth_status_output_supports_lark_cli_nested_user_open_id():
    raw = (
        '{"identities":{"user":{"openId":"ou_nested"},"bot":{"status":"ready"}},'
        '"identity":"user"}'
    )
    assert _parse_auth_status_output(raw)["userOpenId"] == "ou_nested"


def test_parse_auth_status_output_preserves_top_level_user_open_id():
    raw = '{"userOpenId":"ou_top"}'
    assert _parse_auth_status_output(raw) == {"userOpenId": "ou_top"}


def test_parse_auth_status_output_handles_invalid_json():
    assert _parse_auth_status_output("not json") == {}


def test_parse_auth_status_output_handles_null_or_missing_user_identity():
    assert _parse_auth_status_output('{"identities":null}') == {"identities": None}
    assert _parse_auth_status_output('{"identities":{"bot":{"status":"ready"}}}') == {
        "identities": {"bot": {"status": "ready"}}
    }


def test_reply_only_sends_and_does_not_execute(tmp_path):
    cfg = _cfg(tmp_path)
    sent, executed = [], []
    router = FakeRouter(RouteResult(kind="reply", reply="菜单"))
    handle_message(
        _msg(),
        router,
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording(sent),
        execute_fn=lambda *a, **k: executed.append(1),
        thread_factory=ImmediateThread,
    )
    assert executed == []
    assert any("+messages-reply" in cmd for cmd in sent)


def test_execute_path_acks_then_runs_then_reports_and_releases_lock(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    lock = threading.Lock()

    def fake_exec(plan, cfg, task_dir, **kw):
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={"fight": "TT-8 x1"}, error=None)

    handle_message(
        _msg(),
        router,
        cfg,
        lock,
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording(sent),
        execute_fn=fake_exec,
        thread_factory=ImmediateThread,
    )
    lark_msgs = [cmd for cmd in sent if cmd and "lark-cli" in cmd[0]]
    assert len(lark_msgs) >= 2
    assert lock.acquire(blocking=False)
    lock.release()


def test_execute_path_passes_progress_callback(tmp_path):
    cfg = _cfg(tmp_path)
    seen = {}
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))

    def fake_exec(plan, cfg2, task_dir, **kw):
        seen["on_event"] = kw.get("on_event")
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={}, error=None)

    handle_message(
        _msg(),
        router,
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording([]),
        execute_fn=fake_exec,
        thread_factory=ImmediateThread,
    )
    assert callable(seen["on_event"])


def test_progress_disabled_passes_none_callback(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.progress.enable = False
    seen = {}
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))

    def fake_exec(plan, cfg2, task_dir, **kw):
        seen["on_event"] = kw.get("on_event")
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={}, error=None)

    handle_message(
        _msg(),
        router,
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording([]),
        execute_fn=fake_exec,
        thread_factory=ImmediateThread,
    )
    assert seen["on_event"] is None


def test_busy_when_lock_held(tmp_path):
    cfg = _cfg(tmp_path)
    sent, executed = [], []
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    lock = threading.Lock()
    lock.acquire()
    handle_message(
        _msg(),
        router,
        cfg,
        lock,
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording(sent),
        execute_fn=lambda *a, **k: executed.append(1),
        thread_factory=ImmediateThread,
    )
    assert executed == []
    joined = " ".join(" ".join(cmd) for cmd in sent)
    assert cfg.runtime.busy_reply in joined
    lock.release()


def test_running_stop_request_requires_confirmation(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    state = RuntimeState()
    cancel_event = threading.Event()
    state.start_task(cancel_event)
    handle_message(
        Msg("停下来！", "oc_1", "om_1", "ou_1", 0),
        RaisingRouter(),
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording(sent),
        runtime_state=state,
    )
    joined = " ".join(" ".join(cmd) for cmd in sent)
    assert "确定要停止本次 MAA 任务吗" in joined
    assert cancel_event.is_set() is False


def test_running_stop_confirm_sets_cancel_event(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    state = RuntimeState()
    cancel_event = threading.Event()
    state.start_task(cancel_event)
    state.request_stop_confirm()
    handle_message(
        Msg("确认", "oc_1", "om_1", "ou_1", 0),
        RaisingRouter(),
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording(sent),
        runtime_state=state,
    )
    joined = " ".join(" ".join(cmd) for cmd in sent)
    assert "正在停止当前 MAA 任务" in joined
    assert cancel_event.is_set() is True


def test_running_stop_cancel_keeps_task(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    state = RuntimeState()
    cancel_event = threading.Event()
    state.start_task(cancel_event)
    state.request_stop_confirm()
    handle_message(
        Msg("取消", "oc_1", "om_1", "ou_1", 0),
        RaisingRouter(),
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording(sent),
        runtime_state=state,
    )
    joined = " ".join(" ".join(cmd) for cmd in sent)
    assert "继续执行当前任务" in joined
    assert cancel_event.is_set() is False


def test_emulator_status_query_bypasses_router(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []

    def runner(cmd, **kw):
        sent.append(cmd)

        class R:
            returncode = 0
            stdout = "device\n" if cmd[-1] == "get-state" else "{}"
            stderr = ""

        return R()

    handle_message(
        Msg("模拟器开着吗？", "oc_1", "om_1", "ou_1", 0),
        RaisingRouter(),
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=runner,
    )
    joined = " ".join(" ".join(cmd) for cmd in sent)
    assert "模拟器已开启" in joined


def test_worker_exception_replies_error_and_releases_lock(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    lock = threading.Lock()

    def boom(plan, cfg, task_dir, **kw):
        raise RuntimeError("boom")

    handle_message(
        _msg(),
        router,
        cfg,
        lock,
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording(sent),
        execute_fn=boom,
        thread_factory=ImmediateThread,
    )
    assert lock.acquire(blocking=False)
    lock.release()
    joined = " ".join(" ".join(cmd) for cmd in sent)
    assert "执行崩了" in joined


def test_successful_task_sets_service_stop_event(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    service_stop = threading.Event()

    def fake_exec(plan, cfg, task_dir, **kw):
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={}, error=None)

    handle_message(
        _msg(),
        router,
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording([]),
        execute_fn=fake_exec,
        thread_factory=ImmediateThread,
        runtime_state=RuntimeState(),
        service_stop_event=service_stop,
    )
    assert service_stop.is_set() is True


def test_cancelled_task_does_not_set_service_stop_event(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    service_stop = threading.Event()

    def fake_exec(plan, cfg, task_dir, **kw):
        return ExecResult(
            ok=False,
            exit_code=-1,
            raw_log="",
            facts={},
            error="用户已停止本次 MAA 任务",
            cancelled=True,
        )

    handle_message(
        _msg(),
        router,
        cfg,
        threading.Lock(),
        OKLLM(),
        "bot",
        str(tmp_path / "tasks"),
        runner=_runner_recording([]),
        execute_fn=fake_exec,
        thread_factory=ImmediateThread,
        runtime_state=RuntimeState(),
        service_stop_event=service_stop,
    )
    assert service_stop.is_set() is False
