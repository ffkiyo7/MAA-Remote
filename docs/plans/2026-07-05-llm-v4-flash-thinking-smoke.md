# LLM v4-flash thinking smoke results

Date: 2026-07-05

- Environment: `DEEPSEEK_API_KEY` set as a Windows user environment variable.
- Note: the current Codex process did not inherit the newly set variable, so verification commands injected it from the User environment for this run.
- Live API smoke: `deepseek-v4-flash` with `thinking={"type":"enabled"}`, `reasoning_effort="high"`, and JSON mode returned HTTP 200.
- Smoke content: `{"ok": true}`
- Smoke reasoning check: `reasoning_content` was present.
- Router eval: `.venv/Scripts/python -m maa_remote.eval_router` passed `20/20`.
