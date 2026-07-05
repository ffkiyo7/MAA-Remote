from maa_remote.progress import ProgressEvent, ProgressSender, parse_progress_line

ASST_START = 'Assistant::append_callback | TaskChainStart {"taskchain":"Recruit","taskid":2,"uuid":"X"}'
ASST_DONE = 'Assistant::append_callback | TaskChainCompleted {"taskchain":"Recruit","taskid":2,"uuid":"X"}'
ASST_ERROR = 'Assistant::append_callback | TaskChainError {"taskchain":"Fight","taskid":5,"uuid":"X"}'
ASST_SUBTASK = 'Assistant::append_callback | SubTaskStart {"taskchain":"Award","subtask":"ProcessTask"}'


def test_parse_taskchain_start():
    e = parse_progress_line(ASST_START)
    assert e is not None and e.phase == "start" and "公招" in e.text and "中" in e.text


def test_parse_taskchain_completed():
    e = parse_progress_line(ASST_DONE)
    assert e is not None and e.phase == "done" and "✅" in e.text and "公招" in e.text


def test_parse_taskchain_error():
    e = parse_progress_line(ASST_ERROR)
    assert e is not None and e.phase == "error" and "❌" in e.text and "刷理智" in e.text


def test_unknown_chain_falls_back_to_raw_name():
    line = 'x | TaskChainStart {"taskchain":"Roguelike","taskid":9,"uuid":"X"}'
    e = parse_progress_line(line)
    assert e is not None and "Roguelike" in e.text


def test_subtask_and_noise_lines_return_none():
    assert parse_progress_line(ASST_SUBTASK) is None
    assert parse_progress_line("random noise 2026-07-05 [INF]") is None
    assert parse_progress_line("") is None


def test_real_fixture_yields_ordered_chain_events():
    with open("tests/fixtures/maa_stdout_sample.txt", encoding="utf-8") as f:
        lines = f.read().splitlines()
    events = [e for e in (parse_progress_line(l) for l in lines) if e is not None]
    starts = [e for e in events if e.phase == "start"]
    dones = [e for e in events if e.phase == "done"]
    assert len(starts) >= 2, "fixture 里至少应解析出 2 个模块的开始事件"
    assert len(dones) >= 2
    assert any("公招" in e.text for e in events)


def _patched_sender(monkeypatch, calls, anchor="om_anchor", style="thread"):
    def fake_send(message_id, text, identity, runner=None, reply_in_thread=False):
        calls.append((message_id, text, reply_in_thread))
        return "om_new"

    monkeypatch.setattr("maa_remote.progress.send_reply", fake_send)
    return ProgressSender(anchor, "om_trigger", "bot", style=style)


def test_start_sends_immediately_to_thread(monkeypatch):
    calls = []
    s = _patched_sender(monkeypatch, calls)
    s.handle(ProgressEvent("start", "🎫 公招中…"))
    assert calls == [("om_anchor", "🎫 公招中…", True)]


def test_done_buffers_and_merges_with_next_start(monkeypatch):
    calls = []
    s = _patched_sender(monkeypatch, calls)
    s.handle(ProgressEvent("done", "✅ 公招完成"))
    assert calls == []
    s.handle(ProgressEvent("start", "🏗️ 基建换班中…"))
    assert calls == [("om_anchor", "✅ 公招完成 → 🏗️ 基建换班中…", True)]


def test_flush_sends_pending_done(monkeypatch):
    calls = []
    s = _patched_sender(monkeypatch, calls)
    s.handle(ProgressEvent("done", "✅ 刷理智完成"))
    s.flush()
    assert calls == [("om_anchor", "✅ 刷理智完成", True)]
    s.flush()
    assert len(calls) == 1


def test_error_flushes_then_sends(monkeypatch):
    calls = []
    s = _patched_sender(monkeypatch, calls)
    s.handle(ProgressEvent("done", "✅ 公招完成"))
    s.handle(ProgressEvent("error", "❌ 基建换班失败"))
    assert [c[1] for c in calls] == ["✅ 公招完成", "❌ 基建换班失败"]


def test_flat_style_replies_to_trigger(monkeypatch):
    calls = []
    s = _patched_sender(monkeypatch, calls, style="flat")
    s.handle(ProgressEvent("start", "🎫 公招中…"))
    assert calls == [("om_trigger", "🎫 公招中…", False)]


def test_missing_anchor_degrades_to_flat(monkeypatch):
    calls = []
    s = _patched_sender(monkeypatch, calls, anchor=None, style="thread")
    s.handle(ProgressEvent("start", "🎫 公招中…"))
    assert calls == [("om_trigger", "🎫 公招中…", False)]


def test_handle_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("lark down")

    monkeypatch.setattr("maa_remote.progress.send_reply", boom)
    s = ProgressSender("om_anchor", "om_trigger", "bot")
    s.handle(ProgressEvent("start", "🎫 公招中…"))
    s.handle(ProgressEvent("done", "✅ 公招完成"))
    s.flush()
