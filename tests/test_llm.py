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
