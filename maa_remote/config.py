from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from typing import Callable, Mapping


def _expand(path: str, env: Mapping[str, str]) -> str:
    if not path:
        return path
    out = path
    for key, value in env.items():
        out = out.replace(f"%{key}%", value)
    return out


@dataclass
class LarkConfig:
    allowed_sender_open_id: str
    app_id: str
    identity: str
    event_key: str


@dataclass
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    request_timeout_s: int
    max_retries: int
    cache_system_prompt: bool


@dataclass
class FightConfig:
    stage: str
    expiring_medicine: bool
    medicine: int
    stone: int


@dataclass
class MaaConfig:
    maa_cli_path: str
    core_dir: str
    resource_dir: str
    config_dir: str
    asst_log_path: str
    stage_activity_json: str
    client: str
    hot_update_before_catalog: bool
    task_timeout_s: int
    daily_tasks: list[str]
    fight: FightConfig


@dataclass
class EmulatorConfig:
    kind: str
    vmindex: int
    launch_cmd: str
    shutdown_cmd: str
    adb_path: str
    adb_serial: str
    boot_timeout_s: int
    close_after: bool


@dataclass
class RuntimeConfig:
    busy_reply: str
    ack_reply: str
    selection_ttl_s: int
    max_msg_age_s: int
    log_file: str


@dataclass
class ProgressConfig:
    enable: bool
    style: str


@dataclass
class ConfirmConfig:
    mode: str
    ttl_s: int


@dataclass
class Config:
    lark: LarkConfig
    llm: LLMConfig
    maa: MaaConfig
    emulator: EmulatorConfig
    runtime: RuntimeConfig
    progress: ProgressConfig
    confirm: ConfirmConfig


def load_config(path: str, env: Mapping[str, str] | None = None) -> Config:
    env = dict(env) if env is not None else dict(os.environ)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    lark = data["lark"]
    llm = data["llm"]
    maa = data["maa"]
    fight = maa["fight"]
    emulator = data["emulator"]
    runtime = data["runtime"]
    progress = data.get("progress", {})
    confirm = data.get("confirm", {})

    return Config(
        lark=LarkConfig(
            allowed_sender_open_id=lark["allowed_sender_open_id"],
            app_id=lark["app_id"],
            identity=lark["identity"],
            event_key=lark["event_key"],
        ),
        llm=LLMConfig(
            provider=llm["provider"],
            model=llm["model"],
            base_url=llm["base_url"],
            api_key=env.get(llm["api_key_env"], ""),
            request_timeout_s=llm["request_timeout_s"],
            max_retries=llm["max_retries"],
            cache_system_prompt=llm["cache_system_prompt"],
        ),
        maa=MaaConfig(
            maa_cli_path=_expand(maa["maa_cli_path"], env),
            core_dir=_expand(maa["core_dir"], env),
            resource_dir=_expand(maa["resource_dir"], env),
            config_dir=_expand(maa["config_dir"], env),
            asst_log_path=_expand(maa.get("asst_log_path", ""), env),
            stage_activity_json=_expand(maa["stage_activity_json"], env),
            client=maa["client"],
            hot_update_before_catalog=maa["hot_update_before_catalog"],
            task_timeout_s=maa["task_timeout_s"],
            daily_tasks=list(maa["daily_tasks"]),
            fight=FightConfig(
                stage=fight["stage"],
                expiring_medicine=fight["expiring_medicine"],
                medicine=fight["medicine"],
                stone=fight["stone"],
            ),
        ),
        emulator=EmulatorConfig(
            kind=emulator["kind"],
            vmindex=emulator["vmindex"],
            launch_cmd=_expand(emulator["launch_cmd"], env),
            shutdown_cmd=_expand(emulator["shutdown_cmd"], env),
            adb_path=_expand(emulator["adb_path"], env),
            adb_serial=emulator["adb_serial"],
            boot_timeout_s=emulator["boot_timeout_s"],
            close_after=emulator["close_after"],
        ),
        runtime=RuntimeConfig(
            busy_reply=runtime["busy_reply"],
            ack_reply=runtime["ack_reply"],
            selection_ttl_s=runtime["selection_ttl_s"],
            max_msg_age_s=runtime["max_msg_age_s"],
            log_file=runtime["log_file"],
        ),
        progress=ProgressConfig(
            enable=progress.get("enable", True),
            style=progress.get("style", "thread"),
        ),
        confirm=ConfirmConfig(
            mode=confirm.get("mode", "always"),
            ttl_s=confirm.get("ttl_s", 600),
        ),
    )


def resolve_allowed_sender(cfg: Config, auth_status_fn: Callable[[], dict]) -> str:
    if cfg.lark.allowed_sender_open_id:
        return cfg.lark.allowed_sender_open_id

    open_id = auth_status_fn().get("userOpenId", "")
    if not open_id:
        raise RuntimeError(
            "Could not resolve allowed sender from lark-cli auth status. "
            "Set [lark].allowed_sender_open_id explicitly in config.toml."
        )
    return open_id
