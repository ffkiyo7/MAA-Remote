import textwrap
import re

import pytest

from maa_remote.config import load_config, resolve_allowed_sender


def _write(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


_MINIMAL = textwrap.dedent(
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
    maa_cli_path = "%LOCALAPPDATA%/x/maa.exe"
    core_dir = "D:/MAA"
    resource_dir = "D:/MAA/resource"
    config_dir = "%APPDATA%/loong/maa/config"
    stage_activity_json = "%LOCALAPPDATA%/loong/maa/cache/StageActivityV2.json"
    client = "Official"
    hot_update_before_catalog = true
    task_timeout_s = 3600
    daily_tasks = ["startup", "recruit", "fight"]
    [maa.fight]
    stage = ""
    expiring_medicine = true
    medicine = 0
    stone = 0
    [emulator]
    kind = "mumu"
    vmindex = 0
    launch_cmd = '"M M.exe" control -v 0 launch'
    shutdown_cmd = '"M M.exe" control -v 0 shutdown'
    adb_path = "adb.exe"
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


def test_load_config_expands_env_and_reads_key(tmp_path):
    cfg_path = _write(tmp_path, _MINIMAL)
    cfg = load_config(
        cfg_path,
        env={
            "LOCALAPPDATA": "C:/LA",
            "APPDATA": "C:/AD",
            "DEEPSEEK_API_KEY": "sk-xyz",
        },
    )
    assert cfg.maa.maa_cli_path == "C:/LA/x/maa.exe"
    assert cfg.maa.config_dir == "C:/AD/loong/maa/config"
    assert cfg.maa.task_timeout_s == 3600
    assert cfg.llm.api_key == "sk-xyz"
    assert cfg.maa.fight.expiring_medicine is True
    assert cfg.emulator.adb_serial == "127.0.0.1:16384"
    assert cfg.maa.daily_tasks == ["startup", "recruit", "fight"]
    assert cfg.runtime.max_msg_age_s == 300
    assert cfg.runtime.log_file == "logs/maa_remote.log"


def test_resolve_allowed_sender_auto_from_auth(tmp_path):
    cfg = load_config(_write(tmp_path, _MINIMAL), env={"DEEPSEEK_API_KEY": "k"})
    sender = resolve_allowed_sender(cfg, auth_status_fn=lambda: {"userOpenId": "ou_auto"})
    assert sender == "ou_auto"


def test_resolve_allowed_sender_explicit_wins(tmp_path):
    body = _MINIMAL.replace(
        'allowed_sender_open_id = ""', 'allowed_sender_open_id = "ou_fixed"'
    )
    cfg = load_config(_write(tmp_path, body), env={"DEEPSEEK_API_KEY": "k"})
    sender = resolve_allowed_sender(cfg, auth_status_fn=lambda: {"userOpenId": "ou_auto"})
    assert sender == "ou_fixed"


def test_resolve_allowed_sender_fails_fast_when_unresolvable(tmp_path):
    cfg = load_config(_write(tmp_path, _MINIMAL), env={"DEEPSEEK_API_KEY": "k"})
    with pytest.raises(RuntimeError):
        resolve_allowed_sender(cfg, auth_status_fn=lambda: {})


def _example_without(tmp_path, *sections):
    body = open("config.example.toml", encoding="utf-8").read()
    for s in sections:
        body = re.sub(rf"(?ms)^\[{s}\].*?(?=^\[|\Z)", "", body)
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


_ENV = {"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "y"}


def test_progress_and_confirm_defaults_when_sections_missing(tmp_path):
    cfg = load_config(_example_without(tmp_path, "progress", "confirm"), env=_ENV)
    assert cfg.progress.enable is True
    assert cfg.progress.style == "thread"
    assert cfg.confirm.mode == "always"
    assert cfg.confirm.ttl_s == 600


def test_asst_log_path_defaults_empty_and_expands(tmp_path):
    path = _example_without(tmp_path, "progress", "confirm")
    cfg = load_config(path, env=_ENV)
    assert cfg.maa.asst_log_path == ""
    body = re.sub(
        r'(?m)^asst_log_path\s*=.*$',
        'asst_log_path = "%APPDATA%/loong/maa/debug/asst.log"',
        open(path, encoding="utf-8").read(),
        count=1,
    )
    open(path, "w", encoding="utf-8").write(body)
    cfg2 = load_config(path, env=_ENV)
    assert cfg2.maa.asst_log_path == "y/loong/maa/debug/asst.log"


def test_progress_and_confirm_sections_override(tmp_path):
    path = _example_without(tmp_path, "progress", "confirm")
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            '\n[progress]\nenable = false\nstyle = "flat"\n'
            '[confirm]\nmode = "spend_only"\nttl_s = 120\n'
        )
    cfg = load_config(path, env=_ENV)
    assert cfg.progress.enable is False
    assert cfg.progress.style == "flat"
    assert cfg.confirm.mode == "spend_only"
    assert cfg.confirm.ttl_s == 120


def _example_llm_without(tmp_path, *keys):
    body = open("config.example.toml", encoding="utf-8").read()
    lines = [
        line
        for line in body.splitlines()
        if not any(line.strip().startswith(k) for k in keys)
    ]
    p = tmp_path / "config.toml"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def test_llm_thinking_defaults_when_keys_missing(tmp_path):
    path = _example_llm_without(tmp_path, "thinking", "reasoning_effort")
    cfg = load_config(path, env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "y"})
    assert cfg.llm.thinking == "enabled"
    assert cfg.llm.reasoning_effort == "high"


def test_llm_thinking_keys_override(tmp_path):
    path = _example_llm_without(tmp_path, "thinking", "reasoning_effort")
    body = open(path, encoding="utf-8").read().replace(
        "[llm]", '[llm]\nthinking = "disabled"\nreasoning_effort = "max"', 1
    )
    open(path, "w", encoding="utf-8").write(body)
    cfg = load_config(path, env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "y"})
    assert cfg.llm.thinking == "disabled"
    assert cfg.llm.reasoning_effort == "max"
