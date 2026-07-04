import threading
import shutil

from maa_remote.__main__ import _parse_auth_status_output, handle_message, setup_logging
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
