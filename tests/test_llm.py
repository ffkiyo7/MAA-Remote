import pytest

from maa_remote.llm import LLMClient, LLMError


def make_post(capture):
    def _post(url, headers, payload, timeout):
        capture["url"] = url
        capture["headers"] = headers
        capture["payload"] = payload
        capture["timeout"] = timeout
        return {"choices": [{"message": {"content": "hello"}}]}

    return _post


def test_chat_returns_content_and_builds_request():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-chat", 30, post=make_post(cap)
    )
    out = client.chat("SYS", "USER", json_mode=True)
    assert out == "hello"
    assert cap["url"] == "https://api.deepseek.com/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer sk-1"
    assert cap["headers"]["Content-Type"] == "application/json"
    assert cap["payload"]["model"] == "deepseek-chat"
    assert cap["payload"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert cap["payload"]["messages"][1] == {"role": "user", "content": "USER"}
    assert cap["payload"]["response_format"] == {"type": "json_object"}
    assert cap["timeout"] == 30


def test_chat_strips_base_url_slash():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com/", "sk-1", "deepseek-chat", 30, post=make_post(cap)
    )
    client.chat("SYS", "USER")
    assert cap["url"] == "https://api.deepseek.com/chat/completions"


def test_chat_without_json_mode_omits_response_format():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-chat", 30, post=make_post(cap)
    )
    client.chat("SYS", "USER")
    assert "response_format" not in cap["payload"]


def test_chat_raises_on_bad_response():
    def bad_post(url, headers, payload, timeout):
        return {"error": "boom"}

    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-chat", 30, post=bad_post
    )
    with pytest.raises(LLMError):
        client.chat("SYS", "USER")


def test_chat_preserves_llm_error():
    def bad_post(url, headers, payload, timeout):
        raise LLMError("provider rejected request")

    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-chat", 30, post=bad_post
    )
    with pytest.raises(LLMError, match="provider rejected request"):
        client.chat("SYS", "USER")


def test_chat_sends_thinking_and_effort_at_top_level_by_default():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-v4-flash", 120, post=make_post(cap)
    )
    client.chat("SYS", "USER", json_mode=True)
    assert cap["payload"]["thinking"] == {"type": "enabled"}
    assert cap["payload"]["reasoning_effort"] == "high"
    assert "extra_body" not in cap["payload"]
    assert cap["payload"]["response_format"] == {"type": "json_object"}


def test_chat_thinking_disabled_omits_reasoning_effort():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com",
        "sk-1",
        "deepseek-v4-flash",
        120,
        post=make_post(cap),
        thinking="disabled",
    )
    client.chat("SYS", "USER")
    assert cap["payload"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in cap["payload"]


def test_chat_effort_max_passthrough():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com",
        "sk-1",
        "deepseek-v4-flash",
        120,
        post=make_post(cap),
        reasoning_effort="max",
    )
    client.chat("SYS", "USER")
    assert cap["payload"]["reasoning_effort"] == "max"


def test_chat_ignores_reasoning_content_field():
    def post_with_reasoning(url, headers, payload, timeout):
        return {
            "choices": [
                {"message": {"content": '{"action":"run"}', "reasoning_content": "思考过程..."}}
            ]
        }

    client = LLMClient(
        "https://api.deepseek.com",
        "sk-1",
        "deepseek-v4-flash",
        120,
        post=post_with_reasoning,
    )
    assert client.chat("SYS", "USER") == '{"action":"run"}'
