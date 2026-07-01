# MAA_remote Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个常驻单进程服务：飞书 DM 触发 → 意图识别 → 自动拉起 MuMu 模拟器跑 maa-cli 明日方舟日常 → 把结果润色成自然语言回飞书。

**Architecture:** 方案 A 单进程常驻。四段流水线 Listener → Router → Executor → Reporter，串在一个带单飞锁的主循环里。所有外部交互（lark-cli、maa-cli、adb、模拟器、DeepSeek HTTP）都通过可注入的 runner/post 函数封装，便于 TDD mock。意图识别走 DeepSeek（JSON 模式 + schema 强校验），配置/身份/路径全部外置到 `config.toml`。

**Tech Stack:** Python 3.14；stdlib `tomllib`/`subprocess`/`json`/`threading`；`httpx`（DeepSeek HTTP）；`jsonschema`（TaskPlan 校验）；`pytest`（测试）。maa-cli v0.7.5，lark-cli 1.0.63，MuMu 12。

## Global Constraints

- **Python 3.14**；仅用上面列出的第三方依赖（httpx、jsonschema、pytest），其余走 stdlib。
- **零硬编码身份/路径**：appId / open_id / 模拟器路径 / adb 端口 / DeepSeek key 全部来自 `config.toml` 或环境变量；飞书身份运行时读 `lark-cli auth status`，`allowed_sender_open_id` 为空时自动锁定当机登录者。
- **单飞**：同一时刻只跑一个 maa 任务；执行中来消息回 `runtime.busy_reply`，绝不并发。
- **省钱红线**：`fight.stone` 和 `fight.medicine`（囤药）默认 0，只有用户明确要求才 >0；`expiring_medicine`（揉揉乐）默认开。
- **每步兜底**：模拟器/adb/maa/DeepSeek 任一失败都回明确失败消息，绝不静默。
- **DeepSeek key** 从环境变量 `DEEPSEEK_API_KEY` 读，绝不写进任何文件或提交。
- 已有设计文档：`CONTEXT.md`、`SPEC.md`；已有资产：`config.toml`、`config.example.toml`、`prompts/router.system.md`、`schemas/task_plan.schema.json`、`evals/router_cases.jsonl`。实现须与它们一致。
- maa-cli 关键事实：`--expiring-medicine` / `--medicine` / `--stone` 取**数量**（非布尔）；`-a <serial>` 指定 adb 地址；`--dry-run` 只解析配置不连游戏（用于离线校验）；`maa run <task>` 跑 `%APPDATA%/loong/maa/config/tasks/<task>.json` 的自定义任务。
- **落地前置**（非本计划编码范围，但执行前需就绪）：`DEEPSEEK_API_KEY` 已设；maa-cli 已有 MaaCore（`maa install` 或设 `MAA_CORE_DIR` 指向 GUI 目录）；飞书后台已开 `im.message.receive_v1` 订阅且 bot 有 IM 收发权限。

---

### Task 1: 项目脚手架 + 配置加载

**Files:**
- Create: `requirements.txt`, `.gitignore`, `maa_remote/__init__.py`, `maa_remote/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 项目根 `config.toml`（已存在）。
- Produces:
  - `load_config(path: str, env: Mapping[str,str] | None = None) -> Config`
  - `resolve_allowed_sender(cfg: Config, auth_status_fn: Callable[[], dict]) -> str`
  - dataclasses `Config`（字段 `.lark .llm .maa .emulator .runtime`），`LarkConfig`、`LLMConfig`、`MaaConfig`（含 `.fight: FightConfig`）、`EmulatorConfig`、`RuntimeConfig`、`FightConfig`。字段见下方实现。

- [ ] **Step 1: 初始化仓库与依赖清单**

Run（项目根）:
```bash
git init
printf '__pycache__/\n*.pyc\n.venv/\nconfig.toml\n.pytest_cache/\n' > .gitignore
printf 'httpx>=0.27\njsonschema>=4.21\npytest>=8.0\n' > requirements.txt
python -m venv .venv
```
说明：`config.toml` 入 `.gitignore`（机器专属，含本机路径）；`config.example.toml` 保持被跟踪。

- [ ] **Step 2: 写失败测试**

`tests/test_config.py`:
```python
import textwrap
from maa_remote.config import load_config, resolve_allowed_sender

def _write(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)

def test_load_config_expands_env_and_reads_key(tmp_path):
    cfg_path = _write(tmp_path, textwrap.dedent('''
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
        maa_cli_path = "%APPDATA%/x/maa.exe"
        core_dir = "D:/MAA"
        resource_dir = "D:/MAA/resource"
        stage_activity_json = "%APPDATA%/loong/maa/cache/StageActivityV2.json"
        client = "Official"
        hot_update_before_catalog = true
        daily_tasks = ["startup", "recruit"]
        [maa.fight]
        stage = ""
        expiring_medicine = true
        medicine = 0
        stone = 0
        [emulator]
        kind = "mumu"
        vmindex = 0
        launch_cmd = "M.exe control -v 0 launch"
        shutdown_cmd = "M.exe control -v 0 shutdown"
        adb_path = "adb.exe"
        adb_serial = "127.0.0.1:16384"
        boot_timeout_s = 120
        close_after = false
        [runtime]
        busy_reply = "busy"
        ack_reply = "ack"
        selection_ttl_s = 300
    '''))
    cfg = load_config(cfg_path, env={"APPDATA": "C:/AD", "DEEPSEEK_API_KEY": "sk-xyz"})
    assert cfg.maa.maa_cli_path == "C:/AD/x/maa.exe"
    assert cfg.llm.api_key == "sk-xyz"
    assert cfg.maa.fight.expiring_medicine is True
    assert cfg.emulator.adb_serial == "127.0.0.1:16384"
    assert cfg.maa.daily_tasks == ["startup", "recruit"]

def test_resolve_allowed_sender_auto_from_auth(tmp_path):
    cfg_path = _write(tmp_path, _MINIMAL)  # allowed_sender_open_id = ""
    cfg = load_config(cfg_path, env={"DEEPSEEK_API_KEY": "k"})
    sender = resolve_allowed_sender(cfg, auth_status_fn=lambda: {"userOpenId": "ou_auto"})
    assert sender == "ou_auto"

def test_resolve_allowed_sender_explicit_wins(tmp_path):
    cfg_path = _write(tmp_path, _MINIMAL.replace('allowed_sender_open_id = ""', 'allowed_sender_open_id = "ou_fixed"'))
    cfg = load_config(cfg_path, env={"DEEPSEEK_API_KEY": "k"})
    sender = resolve_allowed_sender(cfg, auth_status_fn=lambda: {"userOpenId": "ou_auto"})
    assert sender == "ou_fixed"

_MINIMAL = textwrap.dedent('''
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
    maa_cli_path = "maa.exe"
    core_dir = "D:/MAA"
    resource_dir = "D:/MAA/resource"
    stage_activity_json = "s.json"
    client = "Official"
    hot_update_before_catalog = true
    daily_tasks = ["startup"]
    [maa.fight]
    stage = ""
    expiring_medicine = true
    medicine = 0
    stone = 0
    [emulator]
    kind = "mumu"
    vmindex = 0
    launch_cmd = "l"
    shutdown_cmd = "s"
    adb_path = "adb"
    adb_serial = "127.0.0.1:16384"
    boot_timeout_s = 120
    close_after = false
    [runtime]
    busy_reply = "busy"
    ack_reply = "ack"
    selection_ttl_s = 300
''')
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: maa_remote.config`）

- [ ] **Step 4: 实现 config.py**

`maa_remote/__init__.py`: 空文件。
`maa_remote/config.py`:
```python
from __future__ import annotations
import os, tomllib
from dataclasses import dataclass
from typing import Callable, Mapping

def _expand(p: str, env: Mapping[str, str]) -> str:
    if not p:
        return p
    out = p
    for k, v in env.items():
        out = out.replace(f"%{k}%", v)
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
    stage_activity_json: str
    client: str
    hot_update_before_catalog: bool
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

@dataclass
class Config:
    lark: LarkConfig
    llm: LLMConfig
    maa: MaaConfig
    emulator: EmulatorConfig
    runtime: RuntimeConfig

def load_config(path: str, env: Mapping[str, str] | None = None) -> Config:
    env = dict(env) if env is not None else dict(os.environ)
    with open(path, "rb") as f:
        d = tomllib.load(f)
    lk = d["lark"]
    lm = d["llm"]
    m = d["maa"]
    fg = m["fight"]
    em = d["emulator"]
    rt = d["runtime"]
    return Config(
        lark=LarkConfig(lk["allowed_sender_open_id"], lk["app_id"], lk["identity"], lk["event_key"]),
        llm=LLMConfig(lm["provider"], lm["model"], lm["base_url"],
                      env.get(lm["api_key_env"], ""), lm["request_timeout_s"],
                      lm["max_retries"], lm["cache_system_prompt"]),
        maa=MaaConfig(_expand(m["maa_cli_path"], env), _expand(m["core_dir"], env),
                      _expand(m["resource_dir"], env), _expand(m["stage_activity_json"], env),
                      m["client"], m["hot_update_before_catalog"], list(m["daily_tasks"]),
                      FightConfig(fg["stage"], fg["expiring_medicine"], fg["medicine"], fg["stone"])),
        emulator=EmulatorConfig(em["kind"], em["vmindex"], _expand(em["launch_cmd"], env),
                                _expand(em["shutdown_cmd"], env), _expand(em["adb_path"], env),
                                em["adb_serial"], em["boot_timeout_s"], em["close_after"]),
        runtime=RuntimeConfig(rt["busy_reply"], rt["ack_reply"], rt["selection_ttl_s"]),
    )

def resolve_allowed_sender(cfg: Config, auth_status_fn: Callable[[], dict]) -> str:
    if cfg.lark.allowed_sender_open_id:
        return cfg.lark.allowed_sender_open_id
    return auth_status_fn().get("userOpenId", "")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add .gitignore requirements.txt maa_remote/ tests/test_config.py
git commit -m "feat: config loader with env expansion and lark identity resolution"
```

---

### Task 2: 数据模型

**Files:**
- Create: `maa_remote/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `FightConfig`（Task 1）。
- Produces: dataclasses `Msg`, `StageInfo`, `Fight`, `Recruit`, `Toggle`, `TaskPlan`, `ExecResult`, `RouteResult`；`TaskPlan.from_llm_dict(d: dict, fight_defaults: FightConfig) -> TaskPlan`；`TaskPlan.daily(fight_defaults, daily_tasks: list[str]) -> TaskPlan`。

- [ ] **Step 1: 写失败测试**

`tests/test_models.py`:
```python
from maa_remote.config import FightConfig
from maa_remote.models import TaskPlan, Fight

DEF = FightConfig(stage="", expiring_medicine=True, medicine=0, stone=0)

def test_from_llm_dict_applies_fight_defaults():
    plan = TaskPlan.from_llm_dict({"action": "run", "fight": {"enable": True, "stage": "CE-6", "times": 3}}, DEF)
    assert plan.action == "run"
    assert plan.fight.enable is True
    assert plan.fight.stage == "CE-6"
    assert plan.fight.times == 3
    assert plan.fight.expiring_medicine is True   # 默认继承
    assert plan.fight.medicine == 0 and plan.fight.stone == 0

def test_from_llm_dict_disables_subtask():
    plan = TaskPlan.from_llm_dict({"action": "run", "recruit": {"enable": False}}, DEF)
    assert plan.recruit.enable is False
    assert plan.infrast.enable is True            # 未提及=默认开
    assert plan.startup is True

def test_daily_builds_full_plan():
    plan = TaskPlan.daily(DEF, ["startup", "recruit", "infrast", "mall", "award"])
    assert plan.action == "run"
    assert plan.fight.enable is True and plan.fight.expiring_medicine is True
    assert plan.recruit.enable and plan.mall.enable and plan.award.enable

def test_clarify_carries_question():
    plan = TaskPlan.from_llm_dict({"action": "clarify", "clarify_question": "跑日常还是刷关?"}, DEF)
    assert plan.action == "clarify"
    assert plan.clarify_question == "跑日常还是刷关?"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL（`ModuleNotFoundError: maa_remote.models`）

- [ ] **Step 3: 实现 models.py**

`maa_remote/models.py`:
```python
from __future__ import annotations
from dataclasses import dataclass, field
from maa_remote.config import FightConfig

@dataclass
class Msg:
    text: str
    chat_id: str
    message_id: str
    sender_open_id: str
    create_time: int

@dataclass
class StageInfo:
    activity_name: str
    code: str
    drop: str
    expire_utc: str

@dataclass
class Fight:
    enable: bool = False
    stage: str = ""
    times: int | None = None
    expiring_medicine: bool = True
    medicine: int = 0
    stone: int = 0

@dataclass
class Recruit:
    enable: bool = True
    max_times: int = 4

@dataclass
class Toggle:
    enable: bool = True

@dataclass
class TaskPlan:
    action: str
    startup: bool = True
    recruit: Recruit = field(default_factory=Recruit)
    infrast: Toggle = field(default_factory=Toggle)
    mall: Toggle = field(default_factory=Toggle)
    award: Toggle = field(default_factory=Toggle)
    fight: Fight = field(default_factory=Fight)
    clarify_question: str = ""
    note: str = ""

    @classmethod
    def from_llm_dict(cls, d: dict, fight_defaults: FightConfig) -> "TaskPlan":
        rc = d.get("recruit", {})
        fg = d.get("fight", {})
        fight = Fight(
            enable=fg.get("enable", False),
            stage=fg.get("stage", fight_defaults.stage),
            times=fg.get("times"),
            expiring_medicine=fg.get("expiring_medicine", fight_defaults.expiring_medicine),
            medicine=fg.get("medicine", fight_defaults.medicine),
            stone=fg.get("stone", fight_defaults.stone),
        )
        return cls(
            action=d["action"],
            startup=d.get("startup", True),
            recruit=Recruit(enable=rc.get("enable", True), max_times=rc.get("max_times", 4)),
            infrast=Toggle(enable=d.get("infrast", {}).get("enable", True)),
            mall=Toggle(enable=d.get("mall", {}).get("enable", True)),
            award=Toggle(enable=d.get("award", {}).get("enable", True)),
            fight=fight,
            clarify_question=d.get("clarify_question", ""),
            note=d.get("note", ""),
        )

    @classmethod
    def daily(cls, fight_defaults: FightConfig, daily_tasks: list[str]) -> "TaskPlan":
        return cls(
            action="run",
            startup="startup" in daily_tasks,
            recruit=Recruit(enable="recruit" in daily_tasks),
            infrast=Toggle(enable="infrast" in daily_tasks),
            mall=Toggle(enable="mall" in daily_tasks),
            award=Toggle(enable="award" in daily_tasks),
            fight=Fight(enable=True, stage=fight_defaults.stage,
                        expiring_medicine=fight_defaults.expiring_medicine,
                        medicine=fight_defaults.medicine, stone=fight_defaults.stone),
            note="跑全套日常",
        )

@dataclass
class ExecResult:
    ok: bool
    exit_code: int
    raw_log: str
    facts: dict
    error: str | None = None

@dataclass
class RouteResult:
    kind: str            # "execute" | "reply"
    reply: str | None = None
    plan: TaskPlan | None = None
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_models.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/models.py tests/test_models.py
git commit -m "feat: task plan and message data models"
```

---

### Task 3: DeepSeek LLM 客户端

**Files:**
- Create: `maa_remote/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `class LLMClient(base_url, api_key, model, timeout_s, post=None)`；`post` 签名 `(url:str, headers:dict, payload:dict, timeout:float) -> dict`（返回 OpenAI 兼容响应）。
  - `LLMClient.chat(system: str, user: str, json_mode: bool = False) -> str`
  - `class LLMError(Exception)`

- [ ] **Step 1: 写失败测试**

`tests/test_llm.py`:
```python
import pytest
from maa_remote.llm import LLMClient, LLMError

def make_post(capture):
    def _post(url, headers, payload, timeout):
        capture["url"] = url
        capture["headers"] = headers
        capture["payload"] = payload
        return {"choices": [{"message": {"content": "hello"}}]}
    return _post

def test_chat_returns_content_and_builds_request():
    cap = {}
    c = LLMClient("https://api.deepseek.com", "sk-1", "deepseek-chat", 30, post=make_post(cap))
    out = c.chat("SYS", "USER", json_mode=True)
    assert out == "hello"
    assert cap["url"] == "https://api.deepseek.com/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer sk-1"
    assert cap["payload"]["model"] == "deepseek-chat"
    assert cap["payload"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert cap["payload"]["messages"][1] == {"role": "user", "content": "USER"}
    assert cap["payload"]["response_format"] == {"type": "json_object"}

def test_chat_without_json_mode_omits_response_format():
    cap = {}
    c = LLMClient("https://api.deepseek.com", "sk-1", "deepseek-chat", 30, post=make_post(cap))
    c.chat("SYS", "USER")
    assert "response_format" not in cap["payload"]

def test_chat_raises_on_bad_response():
    def bad_post(url, headers, payload, timeout):
        return {"error": "boom"}
    c = LLMClient("https://api.deepseek.com", "sk-1", "deepseek-chat", 30, post=bad_post)
    with pytest.raises(LLMError):
        c.chat("SYS", "USER")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_llm.py -v`
Expected: FAIL（`ModuleNotFoundError: maa_remote.llm`）

- [ ] **Step 3: 实现 llm.py**

`maa_remote/llm.py`:
```python
from __future__ import annotations
from typing import Callable

class LLMError(Exception):
    pass

def _httpx_post(url, headers, payload, timeout):
    import httpx
    r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()

class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: int,
                 post: Callable[..., dict] | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._post = post or _httpx_post

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            data = self._post(self.base_url + "/chat/completions", headers, payload, self.timeout_s)
            return data["choices"][0]["message"]["content"]
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(str(e)) from e
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_llm.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/llm.py tests/test_llm.py
git commit -m "feat: deepseek llm client with injectable transport"
```

---

### Task 4: StageCatalog（活动关卡）

**Files:**
- Create: `maa_remote/stage_catalog.py`
- Test: `tests/test_stage_catalog.py`, `tests/fixtures/stage_activity_sample.json`

**Interfaces:**
- Consumes: `StageInfo`（Task 2）。
- Produces:
  - `load_open_stages(activity_json_path: str, client: str, now: datetime | None = None) -> list[StageInfo]`
  - `resolve_selection(text: str, stages: list[StageInfo]) -> str | None`（命中返回关卡 code；"取消"返回 `"__cancel__"`；无匹配返回 `None`）
  - `format_menu(stages: list[StageInfo]) -> str`
  - `hot_update(maa_cli_path: str, runner=subprocess.run) -> None`

- [ ] **Step 1: 建 fixture**

`tests/fixtures/stage_activity_sample.json`（模仿真实 `StageActivityV2.json` 结构，含一个开放、一个过期活动）:
```json
{
  "Official": {
    "sideStoryStage": {
      "OPEN": {
        "Activity": {"Tip": "SideStory「测试当期」", "StageName": "测试当期",
          "UtcStartTime": "2026/06/28 16:00:00", "UtcExpireTime": "2026/07/12 03:59:59", "TimeZone": 8},
        "Stages": [
          {"Display": "TT-8", "Value": "TT-8", "Drop": "本关效率最高"},
          {"Display": "TT-7", "Value": "TT-7", "Drop": "突破材料"}
        ]
      },
      "GONE": {
        "Activity": {"Tip": "SideStory「已结束」", "StageName": "已结束",
          "UtcStartTime": "2026/05/01 16:00:00", "UtcExpireTime": "2026/05/15 03:59:59", "TimeZone": 8},
        "Stages": [{"Display": "GG-8", "Value": "GG-8", "Drop": "x"}]
      }
    }
  }
}
```

- [ ] **Step 2: 写失败测试**

`tests/test_stage_catalog.py`:
```python
from datetime import datetime, timezone
from maa_remote.stage_catalog import load_open_stages, resolve_selection, format_menu
from maa_remote.models import StageInfo

FIX = "tests/fixtures/stage_activity_sample.json"
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)   # OPEN 开放中，GONE 已过期

def test_load_open_stages_filters_by_time():
    stages = load_open_stages(FIX, "Official", now=NOW)
    codes = [s.code for s in stages]
    assert codes == ["TT-8", "TT-7"]
    assert stages[0].activity_name == "测试当期"
    assert stages[0].drop == "本关效率最高"

def test_load_open_stages_empty_when_none_open():
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert load_open_stages(FIX, "Official", now=past) == []

def test_resolve_selection_by_index():
    stages = [StageInfo("a", "TT-8", "d", "x"), StageInfo("a", "TT-7", "d", "x")]
    assert resolve_selection("1", stages) == "TT-8"
    assert resolve_selection("2", stages) == "TT-7"

def test_resolve_selection_by_code_and_cancel_and_miss():
    stages = [StageInfo("a", "TT-8", "d", "x")]
    assert resolve_selection("tt-8", stages) == "TT-8"
    assert resolve_selection("取消", stages) == "__cancel__"
    assert resolve_selection("99", stages) is None
    assert resolve_selection("ZZ-9", stages) is None

def test_format_menu_lists_stages():
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    menu = format_menu(stages)
    assert "TT-8" in menu and "本关效率最高" in menu and "1" in menu
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/test_stage_catalog.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 实现 stage_catalog.py**

`maa_remote/stage_catalog.py`:
```python
from __future__ import annotations
import json, subprocess
from datetime import datetime, timezone, timedelta
from maa_remote.models import StageInfo

def _parse_dt(s: str, tz_offset: int) -> datetime:
    dt = datetime.strptime(s, "%Y/%m/%d %H:%M:%S")
    return dt.replace(tzinfo=timezone(timedelta(hours=tz_offset)))

def load_open_stages(activity_json_path: str, client: str, now: datetime | None = None) -> list[StageInfo]:
    now = now or datetime.now(timezone.utc)
    with open(activity_json_path, encoding="utf-8") as f:
        data = json.load(f)
    side = data.get(client, {}).get("sideStoryStage", {})
    out: list[StageInfo] = []
    for ev in side.values():
        act = ev.get("Activity", {})
        if "UtcStartTime" not in act or "UtcExpireTime" not in act:
            continue
        tz = act.get("TimeZone", 8)
        start = _parse_dt(act["UtcStartTime"], tz)
        end = _parse_dt(act["UtcExpireTime"], tz)
        if start <= now <= end:
            for st in ev.get("Stages", []):
                out.append(StageInfo(act.get("StageName", ""), st["Value"],
                                     st.get("Drop", ""), act["UtcExpireTime"]))
    return out

def resolve_selection(text: str, stages: list[StageInfo]) -> str | None:
    t = text.strip()
    if t in ("取消", "cancel", "算了"):
        return "__cancel__"
    if t.isdigit():
        i = int(t) - 1
        return stages[i].code if 0 <= i < len(stages) else None
    for s in stages:
        if t.lower() == s.code.lower():
            return s.code
    return None

def format_menu(stages: list[StageInfo]) -> str:
    act = stages[0].activity_name if stages else ""
    lines = [f"当前活动「{act}」可刷关卡："]
    for i, s in enumerate(stages, 1):
        lines.append(f"{i}. {s.code}（{s.drop}）")
    lines.append("回复编号或关卡号选择，回复「取消」放弃。")
    return "\n".join(lines)

def hot_update(maa_cli_path: str, runner=subprocess.run) -> None:
    runner([maa_cli_path, "hot-update"], capture_output=True, text=True, timeout=120)
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_stage_catalog.py -v`
Expected: PASS（5 passed）

- [ ] **Step 6: Commit**

```bash
git add maa_remote/stage_catalog.py tests/test_stage_catalog.py tests/fixtures/stage_activity_sample.json
git commit -m "feat: stage catalog reads open activity stages and resolves selection"
```

---

### Task 5: Router（意图识别 + 待选状态机）

**Files:**
- Create: `maa_remote/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`Msg`/`TaskPlan`/`RouteResult`（Task 2）、`LLMClient`（Task 3）、`load_open_stages`/`resolve_selection`/`format_menu`（Task 4）、`schemas/task_plan.schema.json`、`prompts/router.system.md`。
- Produces:
  - `class Router(cfg, llm, system_prompt, schema, now_fn=..., stage_loader=load_open_stages, hot_update_fn=None)`
  - `Router.route(msg: Msg) -> RouteResult`

- [ ] **Step 1: 写失败测试**

`tests/test_router.py`:
```python
import json
from datetime import datetime, timezone
from maa_remote.config import load_config
from maa_remote.models import Msg, StageInfo
from maa_remote.router import Router

def _cfg(tmp_path):
    import shutil
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    # 用最小 env，把 example 里的占位路径无所谓（router 不碰它们，stage_loader 被注入）
    return load_config(str(tmp_path / "config.toml"), env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"})

SCHEMA = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))
PROMPT = open("prompts/router.system.md", encoding="utf-8").read()

class FakeLLM:
    def __init__(self, reply): self.reply = reply; self.calls = []
    def chat(self, system, user, json_mode=False):
        self.calls.append((system, user)); return self.reply

def _msg(text): return Msg(text=text, chat_id="oc_1", message_id="om_1", sender_open_id="ou_1", create_time=0)

def test_fast_path_daily_bypasses_llm(tmp_path):
    llm = FakeLLM("SHOULD_NOT_BE_CALLED")
    r = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = r.route(_msg("跑日常"))
    assert rr.kind == "execute"
    assert rr.plan.fight.enable is True and rr.plan.recruit.enable is True
    assert llm.calls == []

def test_llm_path_specific_stage(tmp_path):
    llm = FakeLLM(json.dumps({"action": "run", "recruit": {"enable": False},
                              "fight": {"enable": True, "stage": "CE-6", "times": 3}}))
    r = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = r.route(_msg("打CE-6三次别做公招"))
    assert rr.kind == "execute"
    assert rr.plan.fight.stage == "CE-6" and rr.plan.fight.times == 3
    assert rr.plan.recruit.enable is False

def test_reject_and_clarify_are_reply_only(tmp_path):
    r = Router(_cfg(tmp_path), FakeLLM(json.dumps({"action": "reject", "note": "x"})), PROMPT, SCHEMA)
    assert r.route(_msg("今天天气")).kind == "reply"
    r2 = Router(_cfg(tmp_path), FakeLLM(json.dumps({"action": "clarify", "clarify_question": "跑日常还是刷关?"})), PROMPT, SCHEMA)
    rr = r2.route(_msg("帮我弄一下"))
    assert rr.kind == "reply" and "刷关" in rr.reply

def test_ask_stage_selection_stores_pending_and_replies_menu(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    r = Router(_cfg(tmp_path), FakeLLM(json.dumps({"action": "ask_stage_selection"})), PROMPT, SCHEMA,
               stage_loader=lambda path, client, now=None: stages)
    rr = r.route(_msg("刷这期活动"))
    assert rr.kind == "reply" and "TT-8" in rr.reply
    # 后续消息被当作选择解析
    rr2 = r.route(_msg("1"))
    assert rr2.kind == "execute" and rr2.plan.fight.stage == "TT-8"

def test_ask_stage_selection_empty_replies_no_activity(tmp_path):
    r = Router(_cfg(tmp_path), FakeLLM(json.dumps({"action": "ask_stage_selection"})), PROMPT, SCHEMA,
               stage_loader=lambda path, client, now=None: [])
    rr = r.route(_msg("刷这期活动"))
    assert rr.kind == "reply" and "没有" in rr.reply

def test_invalid_json_then_retry_success(tmp_path):
    class FlakyLLM:
        def __init__(self): self.n = 0
        def chat(self, system, user, json_mode=False):
            self.n += 1
            return "not json" if self.n == 1 else json.dumps({"action": "run", "fight": {"enable": True}})
    r = Router(_cfg(tmp_path), FlakyLLM(), PROMPT, SCHEMA)
    rr = r.route(_msg("刷理智"))
    assert rr.kind == "execute"

def test_invalid_json_exhausts_retries_to_clarify(tmp_path):
    r = Router(_cfg(tmp_path), FakeLLM("still not json"), PROMPT, SCHEMA)
    rr = r.route(_msg("乱七八糟"))
    assert rr.kind == "reply"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_router.py -v`
Expected: FAIL（`ModuleNotFoundError: maa_remote.router`）

- [ ] **Step 3: 实现 router.py**

`maa_remote/router.py`:
```python
from __future__ import annotations
import json, time
from typing import Callable
from jsonschema import validate, ValidationError
from maa_remote.config import Config
from maa_remote.models import Msg, TaskPlan, RouteResult, Fight
from maa_remote.stage_catalog import load_open_stages, resolve_selection, format_menu

FAST_PATH = {"跑日常", "日常", "daily", "跑一下日常", "托管", "托管一下"}

class Router:
    def __init__(self, cfg: Config, llm, system_prompt: str, schema: dict,
                 now_fn: Callable[[], float] = time.time,
                 stage_loader: Callable = load_open_stages,
                 hot_update_fn: Callable | None = None):
        self.cfg = cfg
        self.llm = llm
        self.system_prompt = system_prompt
        self.schema = schema
        self.now_fn = now_fn
        self.stage_loader = stage_loader
        self.hot_update_fn = hot_update_fn
        self._pending: dict[str, tuple[list, float]] = {}   # chat_id -> (stages, expire_at)

    def route(self, msg: Msg) -> RouteResult:
        # 1. 待选状态机
        pend = self._pending.get(msg.chat_id)
        if pend and self.now_fn() < pend[1]:
            return self._handle_selection(msg, pend[0])
        elif pend:
            self._pending.pop(msg.chat_id, None)   # 过期清理

        # 2. 快速路径
        if msg.text.strip() in FAST_PATH:
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply,
                               plan=TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks))

        # 3. LLM 路径
        d = self._llm_plan(msg.text)
        if d is None:
            return RouteResult(kind="reply", reply="没太懂，你是想跑日常还是刷某个具体关卡？")
        action = d.get("action")
        if action == "ask_stage_selection":
            return self._start_selection(msg)
        if action == "clarify":
            return RouteResult(kind="reply", reply=d.get("clarify_question") or "能说得再具体点吗？")
        if action == "reject":
            return RouteResult(kind="reply", reply="这个我帮不上，我只负责跑明日方舟日常/刷关卡～")
        plan = TaskPlan.from_llm_dict(d, self.cfg.maa.fight)
        return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)

    def _llm_plan(self, text: str) -> dict | None:
        user = text
        for attempt in range(self.cfg.llm.max_retries + 1):
            raw = self.llm.chat(self.system_prompt, user, json_mode=True)
            try:
                d = json.loads(raw)
                validate(d, self.schema)
                return d
            except (json.JSONDecodeError, ValidationError) as e:
                user = f"{text}\n\n上次输出无法解析或不符合 schema（{e.__class__.__name__}）。只输出符合 schema 的 JSON。"
        return None

    def _start_selection(self, msg: Msg) -> RouteResult:
        if self.hot_update_fn:
            try:
                self.hot_update_fn(self.cfg.maa.maa_cli_path)
            except Exception:
                pass
        stages = self.stage_loader(self.cfg.maa.stage_activity_json, self.cfg.maa.client)
        if not stages:
            return RouteResult(kind="reply", reply="当前没有开放的活动关卡，要不要刷常规关/当前关？")
        self._pending[msg.chat_id] = (stages, self.now_fn() + self.cfg.runtime.selection_ttl_s)
        return RouteResult(kind="reply", reply=format_menu(stages))

    def _handle_selection(self, msg: Msg, stages: list) -> RouteResult:
        code = resolve_selection(msg.text, stages)
        if code == "__cancel__":
            self._pending.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply="好的，已取消。")
        if code is None:
            return RouteResult(kind="reply", reply="没听懂，回复编号或关卡号，或回复「取消」。")
        self._pending.pop(msg.chat_id, None)
        fd = self.cfg.maa.fight
        plan = TaskPlan(action="run", startup=False,
                        fight=Fight(enable=True, stage=code, expiring_medicine=fd.expiring_medicine,
                                    medicine=fd.medicine, stone=fd.stone),
                        note=f"刷活动关卡 {code}")
        # 只刷选定关卡，不做其它子任务
        plan.recruit.enable = plan.infrast.enable = plan.mall.enable = plan.award.enable = False
        return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_router.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/router.py tests/test_router.py
git commit -m "feat: router with fast-path, llm intent parsing, and stage selection state machine"
```

---

### Task 6: Listener（飞书事件订阅）

**Files:**
- Create: `maa_remote/listener.py`
- Test: `tests/test_listener.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`Msg`（Task 2）。
- Produces:
  - `parse_event(obj: dict, allowed_sender: str) -> Msg | None`
  - `listen(cfg: Config, allowed_sender: str, spawn=subprocess.Popen) -> Iterator[Msg]`

> ⚠️ 落地校验：`parse_event` 的事件外层结构基于飞书 `im.message.receive_v1` 标准 schema 编写。执行前需跑一次 `lark-cli event consume im.message.receive_v1 --as bot` 并 DM 机器人，用真实 NDJSON 核对字段路径，必要时微调 `parse_event`。

- [ ] **Step 1: 写失败测试**

`tests/test_listener.py`:
```python
from maa_remote.listener import parse_event

def _event(text, open_id="ou_1", mtype="text"):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": {"message_id": "om_9", "chat_id": "oc_9", "message_type": mtype,
                        "create_time": "1720000000000",
                        "content": '{"text": "%s"}' % text},
        },
    }

def test_parse_event_extracts_text_message():
    m = parse_event(_event("跑日常"), allowed_sender="ou_1")
    assert m.text == "跑日常" and m.chat_id == "oc_9" and m.message_id == "om_9"
    assert m.sender_open_id == "ou_1" and m.create_time == 1720000000000

def test_parse_event_filters_other_sender():
    assert parse_event(_event("hi", open_id="ou_other"), allowed_sender="ou_1") is None

def test_parse_event_ignores_non_text():
    assert parse_event(_event("x", mtype="image"), allowed_sender="ou_1") is None

def test_parse_event_ignores_non_message_events():
    assert parse_event({"header": {"event_type": "im.message.message_read_v1"}}, "ou_1") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_listener.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 listener.py**

`maa_remote/listener.py`:
```python
from __future__ import annotations
import json, subprocess
from typing import Iterator
from maa_remote.config import Config
from maa_remote.models import Msg

def parse_event(obj: dict, allowed_sender: str) -> Msg | None:
    if obj.get("header", {}).get("event_type") != "im.message.receive_v1":
        return None
    ev = obj.get("event", {})
    message = ev.get("message", {})
    if message.get("message_type") != "text":
        return None
    open_id = ev.get("sender", {}).get("sender_id", {}).get("open_id", "")
    if open_id != allowed_sender:
        return None
    try:
        text = json.loads(message.get("content", "{}")).get("text", "").strip()
    except json.JSONDecodeError:
        return None
    if not text:
        return None
    return Msg(text=text, chat_id=message.get("chat_id", ""),
               message_id=message.get("message_id", ""),
               sender_open_id=open_id, create_time=int(message.get("create_time", "0")))

def listen(cfg: Config, allowed_sender: str, spawn=subprocess.Popen) -> Iterator[Msg]:
    identity = "bot" if cfg.lark.identity in ("auto", "bot") else cfg.lark.identity
    cmd = ["lark-cli", "event", "consume", cfg.lark.event_key, "--as", identity, "--format", "ndjson"]
    proc = spawn(cmd, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = parse_event(obj, allowed_sender)
        if msg:
            yield msg
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_listener.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/listener.py tests/test_listener.py
git commit -m "feat: lark event listener parsing im.message.receive_v1"
```

---

### Task 7: Executor（模拟器 + maa-cli）

**Files:**
- Create: `maa_remote/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`TaskPlan`/`ExecResult`（Task 2）。
- Produces:
  - `build_task_file(plan: TaskPlan, client: str) -> dict`（maa-cli JSON 任务，顶层 `{"tasks": [...]}`）
  - `class EmulatorError(Exception)`
  - `ensure_emulator(cfg, runner=subprocess.run, sleep=time.sleep, monotonic=time.monotonic) -> None`
  - `parse_maa_log(text: str) -> dict`
  - `run_maa(plan, cfg, task_dir: str, runner=subprocess.run) -> ExecResult`
  - `execute(plan, cfg, task_dir: str, runner=subprocess.run, sleep=time.sleep, monotonic=time.monotonic) -> ExecResult`

- [ ] **Step 1: 写失败测试**

`tests/test_executor.py`:
```python
import json, os
import pytest
from maa_remote.config import load_config
from maa_remote.models import TaskPlan, Fight, Recruit, Toggle
from maa_remote.executor import (build_task_file, ensure_emulator, parse_maa_log,
                                  run_maa, execute, EmulatorError)

def _cfg(tmp_path):
    import shutil
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(str(tmp_path / "config.toml"),
                       env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"})

def test_build_task_file_daily_includes_toggled_tasks():
    plan = TaskPlan(action="run", startup=True, recruit=Recruit(enable=True, max_times=4),
                    infrast=Toggle(True), mall=Toggle(True), award=Toggle(True),
                    fight=Fight(enable=True, stage="", expiring_medicine=True, medicine=0, stone=0))
    tf = build_task_file(plan, "Official")
    types = [t["type"] for t in tf["tasks"]]
    assert types == ["StartUp", "Recruit", "Infrast", "Mall", "Award", "Fight"]
    fight = tf["tasks"][-1]
    assert fight["params"]["stage"] == ""
    assert fight["params"]["expiring_medicine"] == 999   # 揉揉乐=用尽即将过期
    assert fight["params"]["medicine"] == 0 and fight["params"]["stone"] == 0

def test_build_task_file_omits_disabled():
    plan = TaskPlan(action="run", startup=False, recruit=Recruit(enable=False),
                    infrast=Toggle(False), mall=Toggle(False), award=Toggle(False),
                    fight=Fight(enable=True, stage="TT-8", times=3))
    tf = build_task_file(plan, "Official")
    types = [t["type"] for t in tf["tasks"]]
    assert types == ["Fight"]
    assert tf["tasks"][0]["params"]["stage"] == "TT-8"
    assert tf["tasks"][0]["params"]["times"] == 3

def test_build_task_file_explicit_medicine_and_stone():
    plan = TaskPlan(action="run", startup=False, recruit=Recruit(enable=False),
                    infrast=Toggle(False), mall=Toggle(False), award=Toggle(False),
                    fight=Fight(enable=True, stage="1-7", medicine=999, stone=50))
    tf = build_task_file(plan, "Official")
    assert tf["tasks"][0]["params"]["medicine"] == 999
    assert tf["tasks"][0]["params"]["stone"] == 50

def test_ensure_emulator_success(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []
    class R:
        def __init__(self, out): self.stdout = out; self.returncode = 0
    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "get-state":
            return R("device\n")
        return R("")
    ensure_emulator(cfg, runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0)
    assert any("launch" in " ".join(c) for c in calls)
    assert any(c[-1] == "get-state" for c in calls)

def test_ensure_emulator_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    class R:
        def __init__(self): self.stdout = "offline\n"; self.returncode = 0
    t = {"v": 0.0}
    def mono():
        t["v"] += 60.0; return t["v"]
    with pytest.raises(EmulatorError):
        ensure_emulator(cfg, runner=lambda cmd, **kw: R(), sleep=lambda s: None, monotonic=mono)

def test_parse_maa_log_extracts_facts():
    log = "理智药已使用 2\n公招识别 4 次\nFight TT-8 完成 3 次\nInfrast 换班完成\n"
    facts = parse_maa_log(log)
    assert facts["raw_tail"]
    # 至少捕获到 fight 次数或 recruit 次数（容错解析）
    assert "3" in facts.get("fight", "") or facts.get("recruit_times") == 4

def test_run_maa_writes_task_and_reports_exit(tmp_path):
    cfg = _cfg(tmp_path)
    task_dir = str(tmp_path / "tasks")
    plan = TaskPlan(action="run", fight=Fight(enable=True, stage="TT-8"))
    class R:
        stdout = "Fight TT-8 完成 1 次\n"; stderr = ""; returncode = 0
    def runner(cmd, **kw):
        assert cfg.maa.maa_cli_path in cmd[0]
        assert "run" in cmd
        assert "-a" in cmd and cfg.emulator.adb_serial in cmd
        return R()
    res = run_maa(plan, cfg, task_dir, runner=runner)
    assert res.ok is True and res.exit_code == 0
    written = os.listdir(task_dir)
    assert any(f.endswith(".json") for f in written)

def test_run_maa_nonzero_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    class R:
        stdout = "boom"; stderr = "err"; returncode = 2
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), runner=lambda cmd, **kw: R())
    assert res.ok is False and res.exit_code == 2 and res.error
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_executor.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 executor.py**

`maa_remote/executor.py`:
```python
from __future__ import annotations
import json, os, re, shlex, subprocess, time, uuid
from maa_remote.config import Config
from maa_remote.models import TaskPlan, ExecResult

class EmulatorError(Exception):
    pass

_EXPIRING_ALL = 999   # 揉揉乐：用尽所有即将过期的理智药

def build_task_file(plan: TaskPlan, client: str) -> dict:
    tasks: list[dict] = []
    if plan.startup:
        tasks.append({"type": "StartUp", "params": {"client_type": client, "start_game_enabled": True}})
    if plan.recruit.enable:
        tasks.append({"type": "Recruit", "params": {
            "refresh": True, "select": [4, 5, 6], "confirm": [3, 4, 5, 6],
            "times": plan.recruit.max_times, "set_time": True, "expedite": False}})
    if plan.infrast.enable:
        tasks.append({"type": "Infrast", "params": {"mode": 0,
            "facility": ["Mfg", "Trade", "Power", "Control", "Reception", "Office", "Dorm"],
            "drones": "_NotUse"}})
    if plan.mall.enable:
        tasks.append({"type": "Mall", "params": {"shopping": True, "buy_first": ["招聘许可", "龙门币"],
            "blacklist": ["碳", "家具"]}})
    if plan.award.enable:
        tasks.append({"type": "Award", "params": {"award": True}})
    if plan.fight.enable:
        params: dict = {
            "stage": plan.fight.stage,
            "expiring_medicine": _EXPIRING_ALL if plan.fight.expiring_medicine else 0,
            "medicine": plan.fight.medicine,
            "stone": plan.fight.stone,
        }
        if plan.fight.times is not None:
            params["times"] = plan.fight.times
        tasks.append({"type": "Fight", "params": params})
    return {"tasks": tasks}

def ensure_emulator(cfg: Config, runner=subprocess.run, sleep=time.sleep, monotonic=time.monotonic) -> None:
    em = cfg.emulator
    runner(shlex.split(em.launch_cmd), capture_output=True, text=True)
    runner([em.adb_path, "connect", em.adb_serial], capture_output=True, text=True)
    deadline = monotonic() + em.boot_timeout_s
    while monotonic() < deadline:
        r = runner([em.adb_path, "-s", em.adb_serial, "get-state"], capture_output=True, text=True)
        if (r.stdout or "").strip() == "device":
            return
        sleep(2)
    raise EmulatorError(f"模拟器/adb 在 {em.boot_timeout_s}s 内未就绪（{em.adb_serial}）")

def parse_maa_log(text: str) -> dict:
    facts: dict = {}
    m = re.search(r"公招[^\d]*(\d+)\s*次", text)
    if m:
        facts["recruit_times"] = int(m.group(1))
    m = re.search(r"Fight\s+(\S+)\s+完成\s+(\d+)\s*次", text)
    if m:
        facts["fight"] = f"{m.group(1)} x{m.group(2)}"
    if "换班完成" in text or "Infrast" in text:
        facts["infrast"] = "已换班"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    facts["raw_tail"] = "\n".join(lines[-15:])
    return facts

def run_maa(plan: TaskPlan, cfg: Config, task_dir: str, runner=subprocess.run) -> ExecResult:
    os.makedirs(task_dir, exist_ok=True)
    name = f"maa_remote_{uuid.uuid4().hex[:8]}"
    task_path = os.path.join(task_dir, name + ".json")
    with open(task_path, "w", encoding="utf-8") as f:
        json.dump(build_task_file(plan, cfg.maa.client), f, ensure_ascii=False, indent=2)
    cmd = [cfg.maa.maa_cli_path, "run", name, "-a", cfg.emulator.adb_serial, "--batch", "--no-summary"]
    env = dict(os.environ)
    env["MAA_CONFIG_DIR"] = os.path.dirname(task_dir)   # tasks 的父目录即 config dir
    try:
        r = runner(cmd, capture_output=True, text=True, env=env, timeout=3600)
    except Exception as e:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=f"maa 启动失败: {e}")
    raw = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
    facts = parse_maa_log(raw)
    if r.returncode != 0:
        return ExecResult(ok=False, exit_code=r.returncode, raw_log=raw, facts=facts,
                          error=f"MAA 非零退出（退出码 {r.returncode}）")
    return ExecResult(ok=True, exit_code=0, raw_log=raw, facts=facts, error=None)

def execute(plan: TaskPlan, cfg: Config, task_dir: str, runner=subprocess.run,
            sleep=time.sleep, monotonic=time.monotonic) -> ExecResult:
    try:
        ensure_emulator(cfg, runner=runner, sleep=sleep, monotonic=monotonic)
    except EmulatorError as e:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=str(e))
    result = run_maa(plan, cfg, task_dir, runner=runner)
    if cfg.emulator.close_after:
        runner(shlex.split(cfg.emulator.shutdown_cmd), capture_output=True, text=True)
    return result
```

> 说明：`Recruit`/`Infrast`/`Mall` 的 `params` 用常见默认值，落地时以 `maa run <name> --dry-run` 校验并按需微调（见 Task 9 落地校验步骤）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_executor.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/executor.py tests/test_executor.py
git commit -m "feat: executor builds maa task file, boots emulator, runs and parses"
```

---

### Task 8: Reporter（汇报）

**Files:**
- Create: `maa_remote/reporter.py`
- Test: `tests/test_reporter.py`

**Interfaces:**
- Consumes: `ExecResult`（Task 2）、`Msg`（Task 2）、`LLMClient`（Task 3）、`Config`（Task 1）。
- Produces:
  - `build_summary(result: ExecResult, note: str, llm) -> str`（LLM 润色，失败回退裸事实模板）
  - `send_reply(message_id: str, text: str, identity: str, runner=subprocess.run) -> None`
  - `report(result: ExecResult, msg: Msg, llm, identity: str, runner=subprocess.run) -> None`

- [ ] **Step 1: 写失败测试**

`tests/test_reporter.py`:
```python
from maa_remote.models import ExecResult, Msg
from maa_remote.reporter import build_summary, send_reply, report

class OKLLM:
    def chat(self, system, user, json_mode=False): return "今天日常跑完啦，公招4次，刷了TT-8三次 ✅"

class BoomLLM:
    def chat(self, system, user, json_mode=False): raise RuntimeError("timeout")

def _res(ok=True):
    return ExecResult(ok=ok, exit_code=0 if ok else 2,
                      raw_log="log tail", facts={"recruit_times": 4, "fight": "TT-8 x3"},
                      error=None if ok else "MAA 非零退出（退出码 2）")

def test_build_summary_uses_llm_when_ok():
    s = build_summary(_res(), "跑日常", OKLLM())
    assert "公招" in s

def test_build_summary_fallback_on_llm_error():
    s = build_summary(_res(), "跑日常", BoomLLM())
    assert "TT-8 x3" in s or "recruit_times" in s or "4" in s

def test_build_summary_failure_result_mentions_error():
    s = build_summary(_res(ok=False), "跑日常", BoomLLM())
    assert "退出码 2" in s

def test_send_reply_invokes_lark_cli():
    calls = []
    class R: returncode = 0; stdout = "{}"; stderr = ""
    def runner(cmd, **kw): calls.append(cmd); return R()
    send_reply("om_1", "hello", "bot", runner=runner)
    cmd = calls[0]
    assert "lark-cli" in cmd[0] and "+messages-reply" in cmd
    assert "--message-id" in cmd and "om_1" in cmd
    assert "--text" in cmd and "hello" in cmd
    assert "--as" in cmd and "bot" in cmd

def test_report_sends_summary():
    sent = {}
    class R: returncode = 0; stdout = "{}"; stderr = ""
    def runner(cmd, **kw):
        sent["cmd"] = cmd; return R()
    msg = Msg(text="跑日常", chat_id="oc_1", message_id="om_1", sender_open_id="ou_1", create_time=0)
    report(_res(), msg, OKLLM(), "bot", runner=runner)
    assert "+messages-reply" in sent["cmd"] and "om_1" in sent["cmd"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_reporter.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 reporter.py**

`maa_remote/reporter.py`:
```python
from __future__ import annotations
import subprocess
from maa_remote.models import ExecResult, Msg

_POLISH_SYS = (
    "你是明日方舟托管助手的汇报员。根据给定的执行事实，用简洁友好的中文口语总结这次跑的结果，"
    "带上关键数字（公招/刷关/基建等），有异常要点明。只输出总结文本，别加多余客套。"
)

def _facts_template(result: ExecResult) -> str:
    lines = ["跑完了。" if result.ok else "跑的过程中出问题了。"]
    f = result.facts or {}
    if "fight" in f:
        lines.append(f"作战：{f['fight']}")
    if "recruit_times" in f:
        lines.append(f"公招：{f['recruit_times']} 次")
    if "infrast" in f:
        lines.append(f"基建：{f['infrast']}")
    if result.error:
        lines.append(f"⚠️ {result.error}")
    return "\n".join(lines)

def build_summary(result: ExecResult, note: str, llm) -> str:
    fallback = _facts_template(result)
    if not result.ok:
        # 失败时也让 LLM 润色，但确保错误信息在内；LLM 失败则用模板
        try:
            user = f"用户意图：{note}\n执行失败，事实：{result.facts}\n错误：{result.error}"
            return llm.chat(_POLISH_SYS, user)
        except Exception:
            return fallback
    try:
        user = f"用户意图：{note}\n执行事实：{result.facts}"
        return llm.chat(_POLISH_SYS, user)
    except Exception:
        return fallback

def send_reply(message_id: str, text: str, identity: str, runner=subprocess.run) -> None:
    cmd = ["lark-cli", "im", "+messages-reply", "--message-id", message_id,
           "--text", text, "--as", identity, "--json"]
    runner(cmd, capture_output=True, text=True)

def report(result: ExecResult, msg: Msg, llm, identity: str, runner=subprocess.run) -> None:
    summary = build_summary(result, msg.text, llm)
    send_reply(msg.message_id, summary, identity, runner=runner)
```

> 注：失败结果的模板 `_facts_template` 一定含 `result.error`（如"退出码 2"）；`build_summary` 对失败结果若 LLM 抛错则回退该模板，测试 `test_build_summary_failure_result_mentions_error` 依赖此路径。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_reporter.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/reporter.py tests/test_reporter.py
git commit -m "feat: reporter polishes result and replies via lark-cli"
```

---

### Task 9: 主循环 + 单飞锁 + 组装

**Files:**
- Create: `maa_remote/__main__.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: 前面所有模块。
- Produces:
  - `handle_message(msg, router, cfg, lock, llm, identity, task_dir, runner, execute_fn) -> None`（可测的单条消息处理，含单飞判定）
  - `main(config_path="config.toml") -> None`（组装并跑主循环）

- [ ] **Step 1: 写失败测试**

`tests/test_main.py`:
```python
import threading, json
from maa_remote.config import load_config
from maa_remote.models import Msg, RouteResult, TaskPlan, Fight, ExecResult
from maa_remote.__main__ import handle_message

def _cfg(tmp_path):
    import shutil
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(str(tmp_path / "config.toml"),
                       env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"})

class FakeRouter:
    def __init__(self, rr): self.rr = rr
    def route(self, msg): return self.rr

class OKLLM:
    def chat(self, system, user, json_mode=False): return "done"

def _msg(): return Msg(text="跑日常", chat_id="oc_1", message_id="om_1", sender_open_id="ou_1", create_time=0)

def test_reply_only_sends_and_does_not_execute(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    def runner(cmd, **kw):
        sent.append(cmd)
        class R: returncode = 0; stdout = "{}"; stderr = ""
        return R()
    executed = []
    router = FakeRouter(RouteResult(kind="reply", reply="菜单"))
    handle_message(_msg(), router, cfg, threading.Lock(), OKLLM(), "bot",
                   str(tmp_path / "tasks"), runner, execute_fn=lambda *a, **k: executed.append(1))
    assert executed == []
    assert any("+messages-send" in c or "+messages-reply" in c for c in sent)

def test_execute_path_acks_then_runs_then_reports(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    def runner(cmd, **kw):
        sent.append(cmd)
        class R: returncode = 0; stdout = "{}"; stderr = ""
        return R()
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    def fake_exec(plan, cfg, task_dir, **kw):
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={"fight": "TT-8 x1"}, error=None)
    handle_message(_msg(), router, cfg, threading.Lock(), OKLLM(), "bot",
                   str(tmp_path / "tasks"), runner, execute_fn=fake_exec)
    # 至少两条飞书消息：ack + 最终汇报
    lark_msgs = [c for c in sent if "im" in c]
    assert len(lark_msgs) >= 2

def test_busy_when_lock_held(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    def runner(cmd, **kw):
        sent.append(cmd)
        class R: returncode = 0; stdout = "{}"; stderr = ""
        return R()
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    lock = threading.Lock()
    lock.acquire()   # 模拟已有任务在跑
    handle_message(_msg(), router, cfg, lock, OKLLM(), "bot",
                   str(tmp_path / "tasks"), runner, execute_fn=lambda *a, **k: None)
    joined = " ".join(" ".join(c) for c in sent)
    assert cfg.runtime.busy_reply in joined
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_main.py -v`
Expected: FAIL（`ModuleNotFoundError: maa_remote.__main__`）

- [ ] **Step 3: 实现 __main__.py**

`maa_remote/__main__.py`:
```python
from __future__ import annotations
import json, os, subprocess, threading
from maa_remote.config import load_config, resolve_allowed_sender, Config
from maa_remote.llm import LLMClient
from maa_remote.listener import listen
from maa_remote.router import Router
from maa_remote.executor import execute as execute_task
from maa_remote.reporter import report, send_reply

def _send_chat(chat_id: str, text: str, identity: str, runner=subprocess.run) -> None:
    runner(["lark-cli", "im", "+messages-send", "--chat-id", chat_id,
            "--text", text, "--as", identity, "--json"], capture_output=True, text=True)

def handle_message(msg, router, cfg: Config, lock: threading.Lock, llm, identity: str,
                   task_dir: str, runner=subprocess.run, execute_fn=execute_task) -> None:
    rr = router.route(msg)
    if rr.kind == "reply":
        send_reply(msg.message_id, rr.reply, identity, runner=runner)
        return
    # kind == "execute"
    if not lock.acquire(blocking=False):
        send_reply(msg.message_id, cfg.runtime.busy_reply, identity, runner=runner)
        return
    try:
        if rr.reply:
            send_reply(msg.message_id, rr.reply, identity, runner=runner)   # ack
        result = execute_fn(rr.plan, cfg, task_dir, runner=runner)
        report(result, msg, llm, identity, runner=runner)
    finally:
        lock.release()

def _auth_status() -> dict:
    out = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}

def main(config_path: str = "config.toml") -> None:
    cfg = load_config(config_path)
    allowed = resolve_allowed_sender(cfg, _auth_status)
    identity = "bot" if cfg.lark.identity in ("auto", "bot") else cfg.lark.identity
    llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model, cfg.llm.request_timeout_s)
    schema = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))
    system_prompt = open("prompts/router.system.md", encoding="utf-8").read()
    from maa_remote.stage_catalog import hot_update
    router = Router(cfg, llm, system_prompt, schema,
                    hot_update_fn=(hot_update if cfg.maa.hot_update_before_catalog else None))
    task_dir = os.path.join(os.path.dirname(cfg.maa.stage_activity_json).replace("cache", "config"), "tasks")
    lock = threading.Lock()
    print(f"[maa_remote] 监听中，允许触发者 open_id={allowed}")
    for msg in listen(cfg, allowed):
        handle_message(msg, router, cfg, lock, llm, identity, task_dir)

if __name__ == "__main__":
    main()
```

> 注：`task_dir` 指向 maa-cli 配置目录下的 `tasks/`（即 `%APPDATA%/loong/maa/config/tasks`）。`main()` 里从 `stage_activity_json`（cache 路径）推导 config 路径；若推导不可靠，可在 `[maa]` 增加显式 `config_dir` 字段——落地时确认。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_main.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 全量测试 + Commit**

Run: `python -m pytest -v`
Expected: 全部 PASS（约 39 项）
```bash
git add maa_remote/__main__.py tests/test_main.py
git commit -m "feat: main loop with single-flight lock wiring all modules"
```

---

### Task 10: 端到端落地校验（真实环境冒烟，不含单元测试）

> 本任务不写代码，是执行前的真实环境校验清单。逐项人工确认，任一失败回到对应模块修正。

**Files:** 无（可能微调 `maa_remote/listener.py` 的 `parse_event` 或 `config.toml`）。

- [ ] **Step 1: 前置就绪**
  - `echo $DEEPSEEK_API_KEY` 非空。
  - maa-cli 有 core：`maa version maa-core` 不报错（否则 `maa install`，或在 `config.toml` 设 `MAA_CORE_DIR` 指向 GUI 目录并在 `run_maa` 的 env 注入）。

- [ ] **Step 2: maa 任务文件离线校验**
  - 手动构造一个 daily plan，跑 `build_task_file` 写出 json 到 `%APPDATA%/loong/maa/config/tasks/smoke.json`。
  - Run: `maa run smoke -a 127.0.0.1:16384 --dry-run`
  - Expected: 解析通过无报错。若某子任务 params 报错，按报错微调 `build_task_file` 对应 params。

- [ ] **Step 3: 模拟器链路**
  - 关掉 MuMu，跑 `ensure_emulator(cfg)`（写个临时脚本），确认能拉起并在超时内返回。
  - Expected: adb `get-state` == `device`。

- [ ] **Step 4: 飞书事件字段核对**
  - Run: `lark-cli event consume im.message.receive_v1 --as bot --format ndjson`，DM 机器人发"跑日常"。
  - 用真实 NDJSON 核对 `parse_event` 字段路径（`event.message.content`/`chat_id`/`sender.sender_id.open_id`/`create_time`）。不一致则修正 `parse_event`，并补一条对应单测。

- [ ] **Step 5: 回复链路**
  - Run: `lark-cli im +messages-send --chat-id <你的P2P chat_id> --text "冒烟测试" --as bot --dry-run` 去掉 `--dry-run` 实发一条，确认收到。

- [ ] **Step 6: 全链路**
  - `python -m maa_remote`，DM 机器人"跑日常"，确认：收到 ack → 模拟器拉起 → maa 跑 → 收到自然语言汇报。
  - Commit（如有 parse_event/config 调整）:
  ```bash
  git add -A && git commit -m "fix: align listener/config with real environment (landing)"
  ```

---

## 落地待采集清单（执行 Task 10 前准备）

1. `DEEPSEEK_API_KEY` 环境变量。
2. maa-cli 的 MaaCore：`maa install` 或复用 GUI core 的方案确认。
3. 你的 P2P chat_id（用于 send 冒烟；reply 用 message_id 不需要）。
4. 飞书后台已开 `im.message.receive_v1` 长连接订阅 + bot IM 收发权限。
