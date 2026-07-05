from __future__ import annotations

from collections.abc import Callable
from typing import Any


class LLMError(Exception):
    pass


def _httpx_post(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    import httpx

    response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: int,
        post: Callable[..., dict[str, Any]] | None = None,
        thinking: str = "enabled",
        reasoning_effort: str = "high",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self._post = post or _httpx_post

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        payload["thinking"] = {"type": self.thinking}
        if self.thinking == "enabled":
            payload["reasoning_effort"] = self.reasoning_effort
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            data = self._post(
                f"{self.base_url}/chat/completions",
                headers,
                payload,
                self.timeout_s,
            )
            return data["choices"][0]["message"]["content"]
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc
