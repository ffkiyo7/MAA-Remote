import json
import shutil

from maa_remote.config import load_config
from maa_remote.listener import listen, parse_event


def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(
        str(tmp_path / "config.toml"),
        env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"},
    )


def _event(text, open_id="ou_1", mtype="text", create_time="1720000000000"):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": {
                "message_id": "om_9",
                "chat_id": "oc_9",
                "message_type": mtype,
                "create_time": create_time,
                "content": '{"text": "%s"}' % text,
            },
        },
    }


def test_parse_event_extracts_text_message():
    msg = parse_event(_event("跑日常"), allowed_sender="ou_1")
    assert msg.text == "跑日常" and msg.chat_id == "oc_9" and msg.message_id == "om_9"
    assert msg.sender_open_id == "ou_1" and msg.create_time == 1720000000000


def test_parse_event_filters_other_sender():
    assert parse_event(_event("hi", open_id="ou_other"), allowed_sender="ou_1") is None


def test_parse_event_ignores_non_text():
    assert parse_event(_event("x", mtype="image"), allowed_sender="ou_1") is None


def test_parse_event_ignores_non_message_events():
    assert parse_event({"header": {"event_type": "im.message.message_read_v1"}}, "ou_1") is None


def test_parse_event_drops_stale_message():
    base = 1720000000000
    event = _event("跑日常", create_time=str(base))
    assert parse_event(event, "ou_1", max_age_s=300, now_ms=base + 301_000) is None
    assert parse_event(event, "ou_1", max_age_s=300, now_ms=base + 299_000) is not None
    assert parse_event(event, "ou_1", max_age_s=0, now_ms=base + 999_000) is not None


def test_parse_event_ignores_invalid_or_empty_content():
    event = _event("跑日常")
    event["event"]["message"]["content"] = "not json"
    assert parse_event(event, "ou_1") is None
    event["event"]["message"]["content"] = '{"text": "   "}'
    assert parse_event(event, "ou_1") is None


def test_listen_restarts_subprocess_after_eof(tmp_path):
    cfg = _cfg(tmp_path)
    event_line = json.dumps(_event("跑日常"))

    class FakeProc:
        def __init__(self, lines):
            self.stdout = iter(lines)

    procs = [FakeProc([]), FakeProc([event_line + "\n"])]
    spawned, slept = [], []

    def spawn(cmd, **kw):
        spawned.append(cmd)
        return procs[len(spawned) - 1]

    gen = listen(cfg, "ou_1", max_age_s=0, spawn=spawn, sleep=slept.append)
    msg = next(gen)
    assert msg.text == "跑日常"
    assert len(spawned) == 2
    assert slept == [1]


def test_listen_builds_lark_cli_command(tmp_path):
    cfg = _cfg(tmp_path)
    event_line = json.dumps(_event("跑日常"))

    class FakeProc:
        def __init__(self):
            self.stdout = iter([event_line + "\n"])

    captured = {}

    def spawn(cmd, **kw):
        captured["cmd"] = cmd
        captured["encoding"] = kw.get("encoding")
        captured["errors"] = kw.get("errors")
        return FakeProc()

    next(listen(cfg, "ou_1", spawn=spawn, sleep=lambda s: None))
    assert captured["cmd"][:4] == ["lark-cli", "event", "consume", "im.message.receive_v1"]
    assert "--as" in captured["cmd"]
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
