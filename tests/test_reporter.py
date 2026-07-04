from maa_remote.models import ExecResult, Msg
from maa_remote.reporter import build_summary, report, send_reply


class OKLLM:
    def chat(self, system, user, json_mode=False):
        return "今天日常跑完啦，公招4次，刷了TT-8三次"


class BoomLLM:
    def chat(self, system, user, json_mode=False):
        raise RuntimeError("timeout")


def _res(ok=True):
    return ExecResult(
        ok=ok,
        exit_code=0 if ok else 2,
        raw_log="log tail",
        facts={"recruit_times": 4, "fight": "TT-8 x3"},
        error=None if ok else "MAA 非零退出（退出码 2）",
    )


def test_build_summary_uses_llm_when_ok():
    summary = build_summary(_res(), "跑日常", OKLLM())
    assert "公招" in summary


def test_build_summary_fallback_on_llm_error():
    summary = build_summary(_res(), "跑日常", BoomLLM())
    assert "TT-8 x3" in summary or "4" in summary


def test_build_summary_failure_result_mentions_error():
    summary = build_summary(_res(ok=False), "跑日常", BoomLLM())
    assert "退出码 2" in summary


def test_send_reply_invokes_lark_cli():
    calls = []

    class R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return R()

    send_reply("om_1", "hello", "bot", runner=runner)
    cmd = calls[0][0]
    assert "lark-cli" in cmd[0] and "+messages-reply" in cmd
    assert "--message-id" in cmd and "om_1" in cmd
    assert "--text" in cmd and "hello" in cmd
    assert "--as" in cmd and "bot" in cmd
    assert calls[0][1]["timeout"] == 30


def test_report_sends_summary():
    sent = {}

    class R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def runner(cmd, **kw):
        sent["cmd"] = cmd
        return R()

    msg = Msg(
        text="跑日常",
        chat_id="oc_1",
        message_id="om_1",
        sender_open_id="ou_1",
        create_time=0,
    )
    report(_res(), msg, OKLLM(), "bot", runner=runner)
    assert "+messages-reply" in sent["cmd"] and "om_1" in sent["cmd"]
