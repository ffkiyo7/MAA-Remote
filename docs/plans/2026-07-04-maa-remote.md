# MAA_remote Implementation Plan（2026-07-04 评审修订版）

> **For agentic workers（Codex 等）：** 按 Task 顺序逐个执行。每个 Step 都要**真的运行命令并核对预期输出**（TDD：先跑失败测试，再实现，再跑通过）。完成一个 Task 就 commit 一次。Step 用 checkbox（`- [ ]`）跟踪。除本计划列出的文件外不要改动仓库里已有的设计文档与契约文件（`CONTEXT.md`/`SPEC.md`/`prompts/`/`schemas/`/`evals/`/`config*.toml`）——实现必须与它们一致，发现冲突以它们为准并停下来报告。

**Goal:** 一个常驻单进程服务：飞书 DM 触发 → 意图识别 → 自动拉起 MuMu 模拟器跑 maa-cli 明日方舟日常 → 把结果润色成自然语言回飞书。

**Architecture:** 方案 A 单进程常驻。四段流水线 Listener → Router → Executor → Reporter；主线程持续监听，**执行跑在 worker 线程**（单飞锁保证同时只有一个任务；执行中来消息立刻回 busy）。所有外部交互（lark-cli、maa-cli、adb、模拟器、DeepSeek HTTP）都通过可注入的 runner/post/spawn 函数封装，便于 TDD mock。意图识别走 DeepSeek（JSON 模式 + schema 强校验），配置/身份/路径全部外置到 `config.toml`。

**Tech Stack:** Python 3.14；stdlib `tomllib`/`subprocess`/`json`/`threading`/`logging`；`httpx`（DeepSeek HTTP）；`jsonschema`（TaskPlan 校验）；`pytest`（测试）。maa-cli v0.7.5，lark-cli 1.0.63，MuMu 12。

## Global Constraints

- **Python 3.14**；仅用三个第三方依赖（httpx、jsonschema、pytest），其余走 stdlib。
- **仓库现状**：git 仓库已初始化、`.gitignore` 已存在且已含 `config.toml`/`logs/`——**不要 `git init`，不要覆盖 `.gitignore`**。`config.toml`（机器专属）不被跟踪，`config.example.toml` 被跟踪。
- **零硬编码身份/路径**：appId / open_id / 模拟器路径 / adb 端口 / DeepSeek key 全部来自 `config.toml` 或环境变量；飞书身份运行时读 `lark-cli auth status`；`allowed_sender_open_id` 为空且自动解析失败时**启动直接报错退出**（绝不带空过滤器静默运行）。
- **单飞 + worker 线程**：执行放后台线程，主线程持续监听；执行中来消息立刻回 `runtime.busy_reply`，绝不并发。锁由主线程 acquire、worker 线程 release（`threading.Lock` 允许）。
- **省钱红线**：`fight.stone` 和 `fight.medicine`（囤药）默认 0；**任何 `stone>0` 或 `medicine>0` 的计划必须先经用户回「确认」才执行**（Router 确认状态机）。
- **StartUp 恒开**：所有可执行 plan 的 `startup=true`（模拟器冷启动后游戏未开，没 StartUp 后续全挂；StartUp 幂等）。
- **旧消息不执行**：`create_time` 距今超过 `runtime.max_msg_age_s` 的消息丢弃并记日志。
- **UTF-8 红线**：Windows 下 subprocess 默认 cp936 会把中文输出弄成乱码/抛异常。所有 `subprocess.run` 走 `maa_remote.procutil.run_utf8`；所有 `subprocess.Popen` 显式 `encoding="utf-8", errors="replace"`。
- **每步兜底**：模拟器/adb/maa/DeepSeek/worker 线程任一失败都回明确失败消息，绝不静默。
- **DeepSeek key** 只从环境变量 `DEEPSEEK_API_KEY` 读，绝不写进任何文件或提交。
- 已有资产（实现必须与之一致）：`CONTEXT.md`、`SPEC.md`、`config.toml`、`config.example.toml`、`prompts/router.system.md`、`schemas/task_plan.schema.json`、`evals/router_cases.jsonl`。
- maa-cli 关键事实：`maa run <task>` 跑 `<config_dir>/tasks/<task>.json` 的自定义任务；`-a <serial>` 指定 adb 地址；`--batch` 非交互；**不要加 `--no-summary`**（结尾 summary 是最好解析的结构化输出）；`--dry-run` 只解析配置不连游戏（离线校验用）；Fight 任务 params 里 `expiring_medicine`/`medicine`/`stone` 取**数量**（非布尔）。
- **落地前置**（非编码范围，Task 12 逐项校验）：`DEEPSEEK_API_KEY` 已设；`MAA_CORE_DIR` 指向 GUI 目录可用；飞书后台已开 `im.message.receive_v1` 订阅且 bot 有 IM 收发权限。

---

### Task 1: 脚手架 + UTF-8 子进程包装 + 配置加载

**Files:**
- Create: `requirements.txt`, `maa_remote/__init__.py`, `maa_remote/procutil.py`, `maa_remote/config.py`
- Test: `tests/test_procutil.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: 项目根 `config.toml` / `config.example.toml`（已存在）。
- Produces:
  - `procutil.run_utf8(cmd, **kw) -> subprocess.CompletedProcess`（默认 `capture_output=True, text=True, encoding="utf-8", errors="replace"`；后续所有模块的 `runner` 参数默认值都是它）
  - `load_config(path: str, env: Mapping[str,str] | None = None) -> Config`
  - `resolve_allowed_sender(cfg: Config, auth_status_fn: Callable[[], dict]) -> str`（解析不到 → **raise RuntimeError**）
  - dataclasses `Config`（字段 `.lark .llm .maa .emulator .runtime`）、`LarkConfig`、`LLMConfig`、`MaaConfig`（含 `.fight: FightConfig`、`.config_dir`、`.task_timeout_s`）、`EmulatorConfig`、`RuntimeConfig`（含 `.max_msg_age_s`、`.log_file`）、`FightConfig`。

- [ ] **Step 1: 建虚拟环境与依赖清单**

Run（项目根；**不要** `git init`、**不要**动已有 `.gitignore`）:
```bash
printf 'httpx>=0.27\njsonschema>=4.21\npytest>=8.0\n' > requirements.txt
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```
Expected: pip 安装成功，无报错。

- [ ] **Step 2: 写失败测试**

`tests/test_procutil.py`:
```python
import sys
from maa_remote.procutil import run_utf8

def test_run_utf8_decodes_chinese_output():
    r = run_utf8([sys.executable, "-c",
                  "import sys; sys.stdout.buffer.write('理智药中文输出'.encode('utf-8'))"])
    assert r.returncode == 0
    assert r.stdout == "理智药中文输出"   # Windows 默认 cp936 会在这里乱码，run_utf8 必须强制 UTF-8

def test_run_utf8_kwargs_passthrough():
    r = run_utf8([sys.executable, "-c", "print('x')"], timeout=30)
    assert r.stdout.strip() == "x"
```

`tests/test_config.py`:
```python
import pytest
import textwrap
from maa_remote.config import load_config, resolve_allowed_sender

def _write(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)

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
''')

def test_load_config_expands_env_and_reads_key(tmp_path):
    cfg_path = _write(tmp_path, _MINIMAL)
    cfg = load_config(cfg_path, env={"LOCALAPPDATA": "C:/LA", "APPDATA": "C:/AD",
                                     "DEEPSEEK_API_KEY": "sk-xyz"})
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
    body = _MINIMAL.replace('allowed_sender_open_id = ""', 'allowed_sender_open_id = "ou_fixed"')
    cfg = load_config(_write(tmp_path, body), env={"DEEPSEEK_API_KEY": "k"})
    sender = resolve_allowed_sender(cfg, auth_status_fn=lambda: {"userOpenId": "ou_auto"})
    assert sender == "ou_fixed"

def test_resolve_allowed_sender_fails_fast_when_unresolvable(tmp_path):
    cfg = load_config(_write(tmp_path, _MINIMAL), env={"DEEPSEEK_API_KEY": "k"})
    with pytest.raises(RuntimeError):
        resolve_allowed_sender(cfg, auth_status_fn=lambda: {})   # 无 userOpenId → 必须炸，不能返回 ""
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_procutil.py tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'maa_remote'`）

- [ ] **Step 4: 实现**

`maa_remote/__init__.py`: 空文件。

`maa_remote/procutil.py`:
```python
from __future__ import annotations
import subprocess

def run_utf8(cmd, **kw):
    """subprocess.run 包装：强制 UTF-8 解码。

    Windows 下 text=True 默认用 cp936（GBK），maa/lark 的中文输出会乱码
    甚至抛 UnicodeDecodeError。全项目统一走这里。
    """
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, **kw)
```

`maa_remote/config.py`:
```python
from __future__ import annotations
import os
import tomllib
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
    config_dir: str
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
                      _expand(m["resource_dir"], env), _expand(m["config_dir"], env),
                      _expand(m["stage_activity_json"], env),
                      m["client"], m["hot_update_before_catalog"], m["task_timeout_s"],
                      list(m["daily_tasks"]),
                      FightConfig(fg["stage"], fg["expiring_medicine"], fg["medicine"], fg["stone"])),
        emulator=EmulatorConfig(em["kind"], em["vmindex"], _expand(em["launch_cmd"], env),
                                _expand(em["shutdown_cmd"], env), _expand(em["adb_path"], env),
                                em["adb_serial"], em["boot_timeout_s"], em["close_after"]),
        runtime=RuntimeConfig(rt["busy_reply"], rt["ack_reply"], rt["selection_ttl_s"],
                              rt["max_msg_age_s"], rt["log_file"]),
    )

def resolve_allowed_sender(cfg: Config, auth_status_fn: Callable[[], dict]) -> str:
    if cfg.lark.allowed_sender_open_id:
        return cfg.lark.allowed_sender_open_id
    open_id = auth_status_fn().get("userOpenId", "")
    if not open_id:
        raise RuntimeError(
            "无法确定允许的触发者：lark-cli auth status 未返回 userOpenId。"
            "请在 config.toml 的 [lark].allowed_sender_open_id 显式填写你的 open_id。"
        )
    return open_id
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_procutil.py tests/test_config.py -v`
Expected: PASS（6 passed）

- [ ] **Step 6: Commit**

```bash
git add requirements.txt maa_remote/ tests/test_procutil.py tests/test_config.py
git commit -m "feat: utf-8 subprocess wrapper and config loader with fail-fast identity resolution"
```

---

### Task 2: 数据模型

**Files:**
- Create: `maa_remote/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `FightConfig`（Task 1）。
- Produces: dataclasses `Msg`, `StageInfo`, `Fight`, `Recruit`, `Toggle`, `TaskPlan`, `ExecResult`, `RouteResult`；`TaskPlan.from_llm_dict(d: dict, fight_defaults: FightConfig) -> TaskPlan`；`TaskPlan.daily(fight_defaults: FightConfig, daily_tasks: list[str]) -> TaskPlan`（**fight 也由 daily_tasks 列表控制**）。

- [ ] **Step 1: 写失败测试**

`tests/test_models.py`:
```python
from maa_remote.config import FightConfig
from maa_remote.models import TaskPlan

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
    assert plan.startup is True                   # 未提及=默认开（StartUp 恒开红线）

def test_daily_builds_full_plan():
    plan = TaskPlan.daily(DEF, ["startup", "recruit", "infrast", "mall", "award", "fight"])
    assert plan.action == "run"
    assert plan.startup is True
    assert plan.fight.enable is True and plan.fight.expiring_medicine is True
    assert plan.recruit.enable and plan.mall.enable and plan.award.enable

def test_daily_fight_controlled_by_task_list():
    plan = TaskPlan.daily(DEF, ["startup", "recruit"])   # 列表里没有 fight
    assert plan.fight.enable is False
    assert plan.infrast.enable is False

def test_clarify_carries_question():
    plan = TaskPlan.from_llm_dict({"action": "clarify", "clarify_question": "跑日常还是刷关?"}, DEF)
    assert plan.action == "clarify"
    assert plan.clarify_question == "跑日常还是刷关?"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_models.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'maa_remote.models'`）

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
    create_time: int          # 毫秒时间戳（飞书事件原样）

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
            fight=Fight(enable="fight" in daily_tasks, stage=fight_defaults.stage,
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

Run: `.venv/Scripts/python -m pytest tests/test_models.py -v`
Expected: PASS（5 passed）

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
  - `class LLMClient(base_url, api_key, model, timeout_s, post=None)`；`post` 签名 `(url: str, headers: dict, payload: dict, timeout: float) -> dict`（返回 OpenAI 兼容响应）。
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

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'maa_remote.llm'`）

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

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v`
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
- Consumes: `StageInfo`（Task 2）、`run_utf8`（Task 1）。
- Produces:
  - `load_open_stages(activity_json_path: str, client: str, now: datetime | None = None) -> list[StageInfo]`
  - `resolve_selection(text: str, stages: list[StageInfo]) -> str | None`（命中返回关卡 code；"取消"返回 `"__cancel__"`；无匹配返回 `None`）
  - `format_menu(stages: list[StageInfo]) -> str`
  - `hot_update(maa_cli_path: str, runner=run_utf8) -> None`

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
from maa_remote.stage_catalog import load_open_stages, resolve_selection, format_menu, hot_update
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

def test_hot_update_invokes_maa_cli():
    calls = []
    hot_update("C:/maa.exe", runner=lambda cmd, **kw: calls.append((cmd, kw)))
    assert calls[0][0] == ["C:/maa.exe", "hot-update"]
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_stage_catalog.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 实现 stage_catalog.py**

`maa_remote/stage_catalog.py`:
```python
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from maa_remote.models import StageInfo
from maa_remote.procutil import run_utf8

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

def hot_update(maa_cli_path: str, runner=run_utf8) -> None:
    runner([maa_cli_path, "hot-update"], timeout=120)
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_stage_catalog.py -v`
Expected: PASS（6 passed）

- [ ] **Step 6: Commit**

```bash
git add maa_remote/stage_catalog.py tests/test_stage_catalog.py tests/fixtures/stage_activity_sample.json
git commit -m "feat: stage catalog reads open activity stages and resolves selection"
```

---

### Task 5: Router（意图识别 + 待选关卡状态机 + 碎石/囤药确认状态机）

**Files:**
- Create: `maa_remote/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`Msg`/`TaskPlan`/`RouteResult`/`Fight`（Task 2）、`LLMClient`（Task 3）、`load_open_stages`/`resolve_selection`/`format_menu`（Task 4）、`schemas/task_plan.schema.json`、`prompts/router.system.md`。
- Produces:
  - `class Router(cfg, llm, system_prompt, schema, now_fn=time.time, stage_loader=load_open_stages, hot_update_fn=None)`
  - `Router.route(msg: Msg) -> RouteResult`
- 关键行为（对应 SPEC §4.2，顺序即优先级）：确认状态机 → 待选状态机 → 快速路径 → LLM 路径；任何将执行的 plan 若 `fight.stone>0 或 fight.medicine>0` → 先要求「确认」；选关产出的 plan **`startup=True`**。

- [ ] **Step 1: 写失败测试**

`tests/test_router.py`:
```python
import json
import shutil
from maa_remote.config import load_config
from maa_remote.models import Msg, StageInfo
from maa_remote.router import Router

def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    # router 不会真的碰这些占位路径（stage_loader 会被注入）
    return load_config(str(tmp_path / "config.toml"),
                       env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"})

SCHEMA = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))
PROMPT = open("prompts/router.system.md", encoding="utf-8").read()

class FakeLLM:
    def __init__(self, reply): self.reply = reply; self.calls = []
    def chat(self, system, user, json_mode=False):
        self.calls.append((system, user)); return self.reply

def _msg(text):
    return Msg(text=text, chat_id="oc_1", message_id="om_1", sender_open_id="ou_1", create_time=0)

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

def test_ask_stage_selection_then_pick_executes_with_startup(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    r = Router(_cfg(tmp_path), FakeLLM(json.dumps({"action": "ask_stage_selection"})), PROMPT, SCHEMA,
               stage_loader=lambda path, client, now=None: stages)
    rr = r.route(_msg("刷这期活动"))
    assert rr.kind == "reply" and "TT-8" in rr.reply
    rr2 = r.route(_msg("1"))                 # 后续消息被当作选择解析
    assert rr2.kind == "execute"
    assert rr2.plan.fight.stage == "TT-8"
    assert rr2.plan.startup is True          # 冷启动红线：选关计划必须带 StartUp
    assert rr2.plan.recruit.enable is False  # 只刷选定关卡

def test_ask_stage_selection_empty_replies_no_activity(tmp_path):
    r = Router(_cfg(tmp_path), FakeLLM(json.dumps({"action": "ask_stage_selection"})), PROMPT, SCHEMA,
               stage_loader=lambda path, client, now=None: [])
    rr = r.route(_msg("刷这期活动"))
    assert rr.kind == "reply" and "没有" in rr.reply

def test_stone_requires_confirmation_then_confirm_executes(tmp_path):
    llm = FakeLLM(json.dumps({"action": "run",
                              "fight": {"enable": True, "stage": "UR-8", "stone": 50}}))
    r = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = r.route(_msg("碎50颗石头刷UR-8"))
    assert rr.kind == "reply" and "确认" in rr.reply and "50" in rr.reply   # 不直接执行
    rr2 = r.route(_msg("确认"))
    assert rr2.kind == "execute" and rr2.plan.fight.stone == 50

def test_stone_confirmation_cancel(tmp_path):
    llm = FakeLLM(json.dumps({"action": "run",
                              "fight": {"enable": True, "stage": "UR-8", "stone": 50}}))
    r = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    r.route(_msg("碎50颗石头刷UR-8"))
    rr = r.route(_msg("取消"))
    assert rr.kind == "reply" and "取消" in rr.reply
    # 确认状态已清空：再说"确认"不会执行（落到 LLM 路径又变成新的确认请求）
    rr2 = r.route(_msg("确认"))
    assert rr2.kind == "reply"

def test_medicine_requires_confirmation(tmp_path):
    llm = FakeLLM(json.dumps({"action": "run",
                              "fight": {"enable": True, "stage": "1-7", "medicine": 999}}))
    r = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = r.route(_msg("把囤药用了刷1-7"))
    assert rr.kind == "reply" and "确认" in rr.reply

def test_confirmation_expires_by_ttl(tmp_path):
    t = {"v": 0.0}
    llm = FakeLLM(json.dumps({"action": "run",
                              "fight": {"enable": True, "stage": "1-7", "medicine": 999}}))
    r = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA, now_fn=lambda: t["v"])
    rr = r.route(_msg("把囤药用了刷1-7"))
    assert rr.kind == "reply" and "确认" in rr.reply
    t["v"] = 301.0                            # selection_ttl_s=300，已过期
    rr2 = r.route(_msg("确认"))
    assert rr2.kind == "reply"                # 过期后「确认」不能放行执行

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

Run: `.venv/Scripts/python -m pytest tests/test_router.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'maa_remote.router'`）

- [ ] **Step 3: 实现 router.py**

`maa_remote/router.py`:
```python
from __future__ import annotations
import json
import time
from typing import Callable
from jsonschema import validate, ValidationError
from maa_remote.config import Config
from maa_remote.models import Msg, TaskPlan, RouteResult, Fight
from maa_remote.stage_catalog import load_open_stages, resolve_selection, format_menu

FAST_PATH = {"跑日常", "日常", "daily", "跑一下日常", "托管", "托管一下"}
CONFIRM_WORDS = {"确认", "确定", "是", "yes", "y"}
CANCEL_WORDS = {"取消", "算了", "不", "no", "n"}

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
        self._pending_selection: dict[str, tuple[list, float]] = {}   # chat_id -> (stages, expire_at)
        self._pending_confirm: dict[str, tuple[TaskPlan, float]] = {}  # chat_id -> (plan, expire_at)

    def route(self, msg: Msg) -> RouteResult:
        # 1. 确认状态机（优先级最高：涉及花钱）
        conf = self._pending_confirm.get(msg.chat_id)
        if conf and self.now_fn() < conf[1]:
            return self._handle_confirm(msg, conf[0])
        elif conf:
            self._pending_confirm.pop(msg.chat_id, None)   # 过期清理

        # 2. 待选关卡状态机
        pend = self._pending_selection.get(msg.chat_id)
        if pend and self.now_fn() < pend[1]:
            return self._handle_selection(msg, pend[0])
        elif pend:
            self._pending_selection.pop(msg.chat_id, None)

        # 3. 快速路径
        if msg.text.strip() in FAST_PATH:
            return self._maybe_confirm(msg, TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks))

        # 4. LLM 路径
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
        return self._maybe_confirm(msg, TaskPlan.from_llm_dict(d, self.cfg.maa.fight))

    def _maybe_confirm(self, msg: Msg, plan: TaskPlan) -> RouteResult:
        """安全阀：碎石/动囤药的计划先要求用户确认，不直接执行。"""
        f = plan.fight
        if f.enable and (f.stone > 0 or f.medicine > 0):
            warns = []
            if f.stone > 0:
                warns.append(f"碎 {f.stone} 颗源石")
            if f.medicine > 0:
                warns.append(f"动用 {f.medicine} 瓶囤积理智药")
            self._pending_confirm[msg.chat_id] = (plan, self.now_fn() + self.cfg.runtime.selection_ttl_s)
            return RouteResult(kind="reply",
                               reply=f"⚠️ 这个计划会{'、'.join(warns)}。回复「确认」执行，回复「取消」放弃。")
        return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)

    def _handle_confirm(self, msg: Msg, plan: TaskPlan) -> RouteResult:
        t = msg.text.strip().lower()
        if t in CONFIRM_WORDS:
            self._pending_confirm.pop(msg.chat_id, None)
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)
        if t in CANCEL_WORDS:
            self._pending_confirm.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply="好的，已取消。")
        return RouteResult(kind="reply", reply="回复「确认」执行这个计划，或「取消」放弃。")

    def _llm_plan(self, text: str) -> dict | None:
        user = text
        for attempt in range(self.cfg.llm.max_retries + 1):
            try:
                raw = self.llm.chat(self.system_prompt, user, json_mode=True)
            except Exception:
                continue
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
                pass   # hot-update 失败用旧缓存，SPEC §6
        stages = self.stage_loader(self.cfg.maa.stage_activity_json, self.cfg.maa.client)
        if not stages:
            return RouteResult(kind="reply", reply="当前没有开放的活动关卡，要不要刷常规关/当前关？")
        self._pending_selection[msg.chat_id] = (stages, self.now_fn() + self.cfg.runtime.selection_ttl_s)
        return RouteResult(kind="reply", reply=format_menu(stages))

    def _handle_selection(self, msg: Msg, stages: list) -> RouteResult:
        code = resolve_selection(msg.text, stages)
        if code == "__cancel__":
            self._pending_selection.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply="好的，已取消。")
        if code is None:
            return RouteResult(kind="reply", reply="没听懂，回复编号或关卡号，或回复「取消」。")
        self._pending_selection.pop(msg.chat_id, None)
        fd = self.cfg.maa.fight
        # startup=True：模拟器多半是冷启动，游戏未开，没 StartUp 后续任务全挂（StartUp 幂等）
        plan = TaskPlan(action="run", startup=True,
                        fight=Fight(enable=True, stage=code, expiring_medicine=fd.expiring_medicine,
                                    medicine=fd.medicine, stone=fd.stone),
                        note=f"刷活动关卡 {code}")
        plan.recruit.enable = plan.infrast.enable = plan.mall.enable = plan.award.enable = False
        return self._maybe_confirm(msg, plan)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_router.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/router.py tests/test_router.py
git commit -m "feat: router with fast-path, llm parsing, stage selection and spend-confirmation state machines"
```

---

### Task 6: Listener（飞书事件订阅：新鲜度过滤 + 断线退避重启）

**Files:**
- Create: `maa_remote/listener.py`
- Test: `tests/test_listener.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`Msg`（Task 2）。
- Produces:
  - `parse_event(obj: dict, allowed_sender: str, max_age_s: int = 0, now_ms: int | None = None) -> Msg | None`（`max_age_s>0` 时丢弃过旧消息；`now_ms=None` 用当前时间）
  - `listen(cfg: Config, allowed_sender: str, max_age_s: int = 0, spawn=subprocess.Popen, sleep=time.sleep) -> Iterator[Msg]`（**内部无限循环**：子进程退出→指数退避 1s→2s→…→封顶 60s 重启；收到有效消息退避归位）

> ⚠️ 落地校验（Task 12）：`parse_event` 的字段路径基于飞书 `im.message.receive_v1` 标准 schema 编写，执行 Task 12 时用真实 NDJSON 核对，必要时微调并补单测。

- [ ] **Step 1: 写失败测试**

`tests/test_listener.py`:
```python
import json
import shutil
from maa_remote.config import load_config
from maa_remote.listener import parse_event, listen

def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(str(tmp_path / "config.toml"),
                       env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"})

def _event(text, open_id="ou_1", mtype="text", create_time="1720000000000"):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": {"message_id": "om_9", "chat_id": "oc_9", "message_type": mtype,
                        "create_time": create_time,
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

def test_parse_event_drops_stale_message():
    base = 1720000000000
    ev = _event("跑日常", create_time=str(base))
    assert parse_event(ev, "ou_1", max_age_s=300, now_ms=base + 301_000) is None       # 超过 5 分钟 → 丢
    assert parse_event(ev, "ou_1", max_age_s=300, now_ms=base + 299_000) is not None   # 未超 → 收
    assert parse_event(ev, "ou_1", max_age_s=0, now_ms=base + 999_000) is not None     # 0 = 不过滤

def test_listen_restarts_subprocess_after_eof(tmp_path):
    cfg = _cfg(tmp_path)
    ev_line = json.dumps(_event("跑日常"))
    class FakeProc:
        def __init__(self, lines): self.stdout = iter(lines)
    procs = [FakeProc([]), FakeProc([ev_line + "\n"])]   # 第一个进程立刻 EOF
    spawned, slept = [], []
    def spawn(cmd, **kw):
        spawned.append(cmd)
        return procs[len(spawned) - 1]
    gen = listen(cfg, "ou_1", max_age_s=0, spawn=spawn, sleep=slept.append)
    msg = next(gen)
    assert msg.text == "跑日常"
    assert len(spawned) == 2          # EOF 后重启了一次
    assert slept == [1]               # 第一次退避 1s

def test_listen_builds_lark_cli_command(tmp_path):
    cfg = _cfg(tmp_path)
    ev_line = json.dumps(_event("跑日常"))
    class FakeProc:
        def __init__(self): self.stdout = iter([ev_line + "\n"])
    captured = {}
    def spawn(cmd, **kw):
        captured["cmd"] = cmd
        captured["encoding"] = kw.get("encoding")
        return FakeProc()
    next(listen(cfg, "ou_1", spawn=spawn, sleep=lambda s: None))
    assert captured["cmd"][:4] == ["lark-cli", "event", "consume", "im.message.receive_v1"]
    assert "--as" in captured["cmd"]
    assert captured["encoding"] == "utf-8"   # UTF-8 红线
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_listener.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 listener.py**

`maa_remote/listener.py`:
```python
from __future__ import annotations
import json
import logging
import subprocess
import time
from typing import Iterator
from maa_remote.config import Config
from maa_remote.models import Msg

log = logging.getLogger(__name__)

def parse_event(obj: dict, allowed_sender: str,
                max_age_s: int = 0, now_ms: int | None = None) -> Msg | None:
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
        create_time = int(message.get("create_time", "0"))
    except ValueError:
        create_time = 0
    if max_age_s > 0:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if now - create_time > max_age_s * 1000:
            log.info("忽略过旧消息 message_id=%s（超过 %ss）", message.get("message_id"), max_age_s)
            return None
    try:
        text = json.loads(message.get("content", "{}")).get("text", "").strip()
    except json.JSONDecodeError:
        return None
    if not text:
        return None
    return Msg(text=text, chat_id=message.get("chat_id", ""),
               message_id=message.get("message_id", ""),
               sender_open_id=open_id, create_time=create_time)

def listen(cfg: Config, allowed_sender: str, max_age_s: int = 0,
           spawn=subprocess.Popen, sleep=time.sleep) -> Iterator[Msg]:
    identity = "bot" if cfg.lark.identity in ("auto", "bot") else cfg.lark.identity
    cmd = ["lark-cli", "event", "consume", cfg.lark.event_key, "--as", identity, "--format", "ndjson"]
    backoff = 1
    while True:   # 断线自愈：子进程退出/异常 → 退避重启
        try:
            proc = spawn(cmd, stdout=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace")
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = parse_event(obj, allowed_sender, max_age_s=max_age_s)
                if msg:
                    backoff = 1   # 有效消息 → 退避归位
                    yield msg
        except Exception:
            log.exception("listener 子进程异常")
        log.warning("lark-cli event consume 退出，%s 秒后重启", backoff)
        sleep(backoff)
        backoff = min(backoff * 2, 60)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_listener.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/listener.py tests/test_listener.py
git commit -m "feat: lark event listener with staleness filter and backoff restart"
```

---

### Task 7: Executor（模拟器 + maa-cli）

**Files:**
- Create: `maa_remote/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`run_utf8`（Task 1）、`TaskPlan`/`ExecResult`（Task 2）。
- Produces:
  - `build_task_file(plan: TaskPlan, client: str) -> dict`（maa-cli JSON 任务，顶层 `{"tasks": [...]}`）
  - `class EmulatorError(Exception)`
  - `ensure_emulator(cfg, runner=run_utf8, sleep=time.sleep, monotonic=time.monotonic) -> None`（**adb connect 在轮询循环内每轮重试**）
  - `parse_maa_log(text: str) -> dict`（优先截取 summary 段，正则兜底，`raw_tail` 保底）
  - `run_maa(plan, cfg, task_dir: str, runner=run_utf8) -> ExecResult`（env 注入 `MAA_CONFIG_DIR`/`MAA_CORE_DIR`/`MAA_RESOURCE_DIR`；超时 `cfg.maa.task_timeout_s`；**不加 `--no-summary`**）
  - `execute(plan, cfg, task_dir: str, runner=run_utf8, sleep=time.sleep, monotonic=time.monotonic) -> ExecResult`

- [ ] **Step 1: 写失败测试**

`tests/test_executor.py`:
```python
import json
import os
import textwrap
import pytest
from maa_remote.config import load_config
from maa_remote.models import TaskPlan, Fight, Recruit, Toggle
from maa_remote.executor import (build_task_file, ensure_emulator, parse_maa_log,
                                  run_maa, execute, EmulatorError)

# 独立 config：launch_cmd 的 exe 路径带空格且加了引号（真实场景 D:/Program Files/...）
_CONFIG = textwrap.dedent('''
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
    maa_cli_path = "C:/maa/maa.exe"
    core_dir = "D:/MAA-GUI"
    resource_dir = "D:/MAA-GUI/resource"
    config_dir = "C:/loong/config"
    stage_activity_json = "C:/loong/cache/StageActivityV2.json"
    client = "Official"
    hot_update_before_catalog = true
    task_timeout_s = 3600
    daily_tasks = ["startup", "recruit", "infrast", "mall", "award", "fight"]
    [maa.fight]
    stage = ""
    expiring_medicine = true
    medicine = 0
    stone = 0
    [emulator]
    kind = "mumu"
    vmindex = 0
    launch_cmd = '"C:/Program Files/Mu Mu/MuMuManager.exe" control -v 0 launch'
    shutdown_cmd = '"C:/Program Files/Mu Mu/MuMuManager.exe" control -v 0 shutdown'
    adb_path = "C:/Program Files/Mu Mu/adb.exe"
    adb_serial = "127.0.0.1:16384"
    boot_timeout_s = 120
    close_after = false
    [runtime]
    busy_reply = "busy"
    ack_reply = "ack"
    selection_ttl_s = 300
    max_msg_age_s = 300
    log_file = "logs/maa_remote.log"
''')

def _cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_CONFIG, encoding="utf-8")
    return load_config(str(p), env={"DEEPSEEK_API_KEY": "k"})

class R:
    def __init__(self, out="", code=0):
        self.stdout = out
        self.stderr = ""
        self.returncode = code

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

def test_ensure_emulator_splits_quoted_spaced_path(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        return R("device\n") if cmd[-1] == "get-state" else R()
    ensure_emulator(cfg, runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0)
    # 引号内含空格的 exe 路径必须是一个 argv 元素——没引号会被切成 "C:/Program" + "Files/..."
    assert calls[0] == ["C:/Program Files/Mu Mu/MuMuManager.exe", "control", "-v", "0", "launch"]

def test_ensure_emulator_retries_connect_each_poll(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []
    state = {"polls": 0}
    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "get-state":
            state["polls"] += 1
            return R("device\n" if state["polls"] >= 3 else "offline\n")
        return R()
    ensure_emulator(cfg, runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0)
    connects = [c for c in calls if len(c) >= 2 and c[1] == "connect"]
    assert len(connects) >= 3   # MuMu 端口后开：connect 必须每轮重试，不能只连一次

def test_ensure_emulator_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    t = {"v": 0.0}
    def mono():
        t["v"] += 60.0
        return t["v"]
    with pytest.raises(EmulatorError):
        ensure_emulator(cfg, runner=lambda cmd, **kw: R("offline\n"),
                        sleep=lambda s: None, monotonic=mono)

def test_parse_maa_log_extracts_summary_section():
    log = "噪音行\n[INFO] Summary\nFight TT-8 3 times\n公招识别 4 次\n"
    facts = parse_maa_log(log)
    assert "Fight TT-8" in facts["summary"]      # summary 段被整体截取
    assert facts["recruit_times"] == 4
    assert facts["raw_tail"]

def test_run_maa_writes_task_and_injects_env(tmp_path):
    cfg = _cfg(tmp_path)
    task_dir = str(tmp_path / "tasks")
    plan = TaskPlan(action="run", fight=Fight(enable=True, stage="TT-8"))
    captured = {}
    def runner(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        captured["timeout"] = kw.get("timeout")
        return R("Summary\nFight TT-8 1 times\n")
    res = run_maa(plan, cfg, task_dir, runner=runner)
    assert res.ok is True and res.exit_code == 0
    assert captured["cmd"][0] == cfg.maa.maa_cli_path
    assert "run" in captured["cmd"] and "--batch" in captured["cmd"]
    assert "--no-summary" not in captured["cmd"]           # summary 要保留（最好解析的输出）
    assert "-a" in captured["cmd"] and cfg.emulator.adb_serial in captured["cmd"]
    assert captured["env"]["MAA_CORE_DIR"] == "D:/MAA-GUI"           # 复用 GUI core 的关键
    assert captured["env"]["MAA_RESOURCE_DIR"] == "D:/MAA-GUI/resource"
    assert captured["env"]["MAA_CONFIG_DIR"] == os.path.dirname(task_dir)
    assert captured["timeout"] == 3600
    assert any(f.endswith(".json") for f in os.listdir(task_dir))

def test_run_maa_nonzero_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), runner=lambda cmd, **kw: R("boom", code=2))
    assert res.ok is False and res.exit_code == 2 and res.error

def test_execute_emulator_failure_short_circuits(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    t = {"v": 0.0}
    def mono():
        t["v"] += 60.0
        return t["v"]
    res = execute(plan, cfg, str(tmp_path / "tasks"),
                  runner=lambda cmd, **kw: R("offline\n"), sleep=lambda s: None, monotonic=mono)
    assert res.ok is False and "未就绪" in res.error
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_executor.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 executor.py**

`maa_remote/executor.py`:
```python
from __future__ import annotations
import json
import os
import re
import shlex
import time
import uuid
from maa_remote.config import Config
from maa_remote.models import TaskPlan, ExecResult
from maa_remote.procutil import run_utf8

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

def ensure_emulator(cfg: Config, runner=run_utf8, sleep=time.sleep, monotonic=time.monotonic) -> None:
    em = cfg.emulator
    # config 里 exe 路径带引号，shlex.split 才能把含空格路径保成一个 argv 元素
    runner(shlex.split(em.launch_cmd), timeout=60)
    deadline = monotonic() + em.boot_timeout_s
    while monotonic() < deadline:
        # connect 放循环内：MuMu 冷启动时 adb 端口后开，只连一次会永远等不到 device
        runner([em.adb_path, "connect", em.adb_serial], timeout=15)
        r = runner([em.adb_path, "-s", em.adb_serial, "get-state"], timeout=15)
        if (r.stdout or "").strip() == "device":
            return
        sleep(2)
    raise EmulatorError(f"模拟器/adb 在 {em.boot_timeout_s}s 内未就绪（{em.adb_serial}）")

def parse_maa_log(text: str) -> dict:
    facts: dict = {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if "summary" in ln.lower():
            facts["summary"] = "\n".join(lines[i:i + 40])   # summary 段整体给 LLM 润色
            break
    m = re.search(r"公招[^\d]*(\d+)\s*次", text)
    if m:
        facts["recruit_times"] = int(m.group(1))
    m = re.search(r"Fight\s+(\S+)[^\d]*(\d+)", text)
    if m:
        facts["fight"] = f"{m.group(1)} x{m.group(2)}"
    if "换班完成" in text or "Infrast" in text:
        facts["infrast"] = "已换班"
    facts["raw_tail"] = "\n".join(lines[-15:])
    return facts

def run_maa(plan: TaskPlan, cfg: Config, task_dir: str, runner=run_utf8) -> ExecResult:
    os.makedirs(task_dir, exist_ok=True)
    name = f"maa_remote_{uuid.uuid4().hex[:8]}"
    task_path = os.path.join(task_dir, name + ".json")
    with open(task_path, "w", encoding="utf-8") as f:
        json.dump(build_task_file(plan, cfg.maa.client), f, ensure_ascii=False, indent=2)
    cmd = [cfg.maa.maa_cli_path, "run", name, "-a", cfg.emulator.adb_serial, "--batch"]
    env = dict(os.environ)
    env["MAA_CONFIG_DIR"] = os.path.dirname(task_dir)
    if cfg.maa.core_dir:
        env["MAA_CORE_DIR"] = cfg.maa.core_dir       # maa-cli 自身没装 core，复用 GUI 的
    if cfg.maa.resource_dir:
        env["MAA_RESOURCE_DIR"] = cfg.maa.resource_dir   # 不识别则无害，Task 12 dry-run 校验
    try:
        r = runner(cmd, env=env, timeout=cfg.maa.task_timeout_s)
    except Exception as e:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=f"maa 启动失败: {e}")
    raw = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
    facts = parse_maa_log(raw)
    if r.returncode != 0:
        return ExecResult(ok=False, exit_code=r.returncode, raw_log=raw, facts=facts,
                          error=f"MAA 非零退出（退出码 {r.returncode}）")
    return ExecResult(ok=True, exit_code=0, raw_log=raw, facts=facts, error=None)

def execute(plan: TaskPlan, cfg: Config, task_dir: str, runner=run_utf8,
            sleep=time.sleep, monotonic=time.monotonic) -> ExecResult:
    try:
        ensure_emulator(cfg, runner=runner, sleep=sleep, monotonic=monotonic)
    except EmulatorError as e:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=str(e))
    result = run_maa(plan, cfg, task_dir, runner=runner)
    if cfg.emulator.close_after:
        runner(shlex.split(cfg.emulator.shutdown_cmd), timeout=60)
    return result
```

> 说明：`Recruit`/`Infrast`/`Mall` 的 `params` 用常见默认值，Task 12 会用 `maa run <name> --dry-run` 校验并按报错微调。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_executor.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/executor.py tests/test_executor.py
git commit -m "feat: executor with quoted-path launch, adb connect retry, core env injection"
```

---

### Task 8: Reporter（汇报）

**Files:**
- Create: `maa_remote/reporter.py`
- Test: `tests/test_reporter.py`

**Interfaces:**
- Consumes: `ExecResult`/`Msg`（Task 2）、`LLMClient`（Task 3）、`run_utf8`（Task 1）。
- Produces:
  - `build_summary(result: ExecResult, note: str, llm) -> str`（LLM 润色，失败回退裸事实模板；失败结果的模板必含 `result.error`）
  - `send_reply(message_id: str, text: str, identity: str, runner=run_utf8) -> None`
  - `report(result: ExecResult, msg: Msg, llm, identity: str, runner=run_utf8) -> None`

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
    assert "TT-8 x3" in s or "4" in s   # 裸事实模板，不静默

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

Run: `.venv/Scripts/python -m pytest tests/test_reporter.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 reporter.py**

`maa_remote/reporter.py`:
```python
from __future__ import annotations
from maa_remote.models import ExecResult, Msg
from maa_remote.procutil import run_utf8

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
    fallback = _facts_template(result)   # 失败结果的模板必含 result.error
    if result.ok:
        user = f"用户意图：{note}\n执行事实：{result.facts}"
    else:
        user = f"用户意图：{note}\n执行失败，事实：{result.facts}\n错误：{result.error}"
    try:
        return llm.chat(_POLISH_SYS, user)
    except Exception:
        return fallback   # LLM 润色失败回退裸事实，不静默

def send_reply(message_id: str, text: str, identity: str, runner=run_utf8) -> None:
    cmd = ["lark-cli", "im", "+messages-reply", "--message-id", message_id,
           "--text", text, "--as", identity, "--json"]
    runner(cmd, timeout=30)

def report(result: ExecResult, msg: Msg, llm, identity: str, runner=run_utf8) -> None:
    summary = build_summary(result, msg.text, llm)
    send_reply(msg.message_id, summary, identity, runner=runner)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_reporter.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/reporter.py tests/test_reporter.py
git commit -m "feat: reporter polishes result and replies via lark-cli"
```

---

### Task 9: 主循环（worker 线程 + 单飞锁 + 日志 + 组装）

**Files:**
- Create: `maa_remote/__main__.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: 前面所有模块。
- Produces:
  - `setup_logging(log_file: str) -> None`（RotatingFileHandler + 控制台，UTF-8）
  - `handle_message(msg, router, cfg, lock, llm, identity, task_dir, runner=run_utf8, execute_fn=execute, thread_factory=threading.Thread) -> None`
  - `main(config_path="config.toml") -> None`

> ⚠️ 架构要点（这是 2026-07-04 评审修掉的最大的坑）：执行**必须**放 worker 线程。
> 若执行和监听同线程串行，`busy_reply` 永远不触发（锁在同线程内早已释放），
> 执行期间积压的消息会在跑完后被依次当作新指令执行。
> 锁由主线程 `acquire`、worker 线程 `release`——`threading.Lock` 允许跨线程 release。

- [ ] **Step 1: 写失败测试**

`tests/test_main.py`:
```python
import shutil
import threading
from maa_remote.config import load_config
from maa_remote.models import Msg, RouteResult, TaskPlan, Fight, ExecResult
from maa_remote.__main__ import handle_message

def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(str(tmp_path / "config.toml"),
                       env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"})

class FakeRouter:
    def __init__(self, rr): self.rr = rr
    def route(self, msg): return self.rr

class OKLLM:
    def chat(self, system, user, json_mode=False): return "done"

class ImmediateThread:
    """同步执行的 Thread 替身：start() 直接跑 target，便于断言执行结果。"""
    def __init__(self, target, daemon=None): self._target = target
    def start(self): self._target()

def _msg():
    return Msg(text="跑日常", chat_id="oc_1", message_id="om_1", sender_open_id="ou_1", create_time=0)

def _runner_recording(sent):
    def runner(cmd, **kw):
        sent.append(cmd)
        class R: returncode = 0; stdout = "{}"; stderr = ""
        return R()
    return runner

def test_reply_only_sends_and_does_not_execute(tmp_path):
    cfg = _cfg(tmp_path)
    sent, executed = [], []
    router = FakeRouter(RouteResult(kind="reply", reply="菜单"))
    handle_message(_msg(), router, cfg, threading.Lock(), OKLLM(), "bot",
                   str(tmp_path / "tasks"), runner=_runner_recording(sent),
                   execute_fn=lambda *a, **k: executed.append(1),
                   thread_factory=ImmediateThread)
    assert executed == []
    assert any("+messages-reply" in c for c in sent)

def test_execute_path_acks_then_runs_then_reports_and_releases_lock(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    lock = threading.Lock()
    def fake_exec(plan, cfg, task_dir, **kw):
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={"fight": "TT-8 x1"}, error=None)
    handle_message(_msg(), router, cfg, lock, OKLLM(), "bot",
                   str(tmp_path / "tasks"), runner=_runner_recording(sent),
                   execute_fn=fake_exec, thread_factory=ImmediateThread)
    lark_msgs = [c for c in sent if c and "lark-cli" in c[0]]
    assert len(lark_msgs) >= 2                    # ack + 最终汇报
    assert lock.acquire(blocking=False)           # 执行完锁必须已释放
    lock.release()

def test_busy_when_lock_held(tmp_path):
    cfg = _cfg(tmp_path)
    sent, executed = [], []
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    lock = threading.Lock()
    lock.acquire()   # 模拟已有任务在跑
    handle_message(_msg(), router, cfg, lock, OKLLM(), "bot",
                   str(tmp_path / "tasks"), runner=_runner_recording(sent),
                   execute_fn=lambda *a, **k: executed.append(1),
                   thread_factory=ImmediateThread)
    assert executed == []                         # busy 时绝不执行
    joined = " ".join(" ".join(c) for c in sent)
    assert cfg.runtime.busy_reply in joined

def test_worker_exception_replies_error_and_releases_lock(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))
    lock = threading.Lock()
    def boom(plan, cfg, task_dir, **kw):
        raise RuntimeError("boom")
    handle_message(_msg(), router, cfg, lock, OKLLM(), "bot",
                   str(tmp_path / "tasks"), runner=_runner_recording(sent),
                   execute_fn=boom, thread_factory=ImmediateThread)
    assert lock.acquire(blocking=False)           # 崩了也必须释放锁
    lock.release()
    joined = " ".join(" ".join(c) for c in sent)
    assert "执行崩了" in joined                   # 崩溃有回音，不静默
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_main.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'maa_remote.__main__'`）

- [ ] **Step 3: 实现 __main__.py**

`maa_remote/__main__.py`:
```python
from __future__ import annotations
import json
import logging
import logging.handlers
import os
import threading
from maa_remote.config import load_config, resolve_allowed_sender, Config
from maa_remote.llm import LLMClient
from maa_remote.listener import listen
from maa_remote.router import Router
from maa_remote.executor import execute as execute_task
from maa_remote.reporter import report, send_reply
from maa_remote.procutil import run_utf8

log = logging.getLogger("maa_remote")

def setup_logging(log_file: str) -> None:
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console],
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

def handle_message(msg, router, cfg: Config, lock: threading.Lock, llm, identity: str,
                   task_dir: str, runner=run_utf8, execute_fn=execute_task,
                   thread_factory=threading.Thread) -> None:
    rr = router.route(msg)
    if rr.kind == "reply":
        send_reply(msg.message_id, rr.reply, identity, runner=runner)
        return
    # kind == "execute"：主线程抢锁，抢不到说明有任务在跑 → 立刻回 busy
    if not lock.acquire(blocking=False):
        send_reply(msg.message_id, cfg.runtime.busy_reply, identity, runner=runner)
        return
    if rr.reply:
        send_reply(msg.message_id, rr.reply, identity, runner=runner)   # ack
    def _job():
        try:
            result = execute_fn(rr.plan, cfg, task_dir, runner=runner)
            report(result, msg, llm, identity, runner=runner)
        except Exception as e:
            log.exception("worker 执行未捕获异常")
            send_reply(msg.message_id, f"执行崩了：{e}", identity, runner=runner)
        finally:
            lock.release()   # threading.Lock 允许跨线程 release
    thread_factory(target=_job, daemon=True).start()

def _auth_status() -> dict:
    # 输出格式在 Task 12 用真机核对；解析不出 JSON 时返回 {}，
    # 上层 resolve_allowed_sender 会报错退出并提示显式配置。
    r = run_utf8(["lark-cli", "auth", "status"], timeout=30)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}

def main(config_path: str = "config.toml") -> None:
    cfg = load_config(config_path)
    setup_logging(cfg.runtime.log_file)
    allowed = resolve_allowed_sender(cfg, _auth_status)   # 解析不到 → RuntimeError，进程退出
    identity = "bot" if cfg.lark.identity in ("auto", "bot") else cfg.lark.identity
    llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model, cfg.llm.request_timeout_s)
    schema = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))
    system_prompt = open("prompts/router.system.md", encoding="utf-8").read()
    from maa_remote.stage_catalog import hot_update
    router = Router(cfg, llm, system_prompt, schema,
                    hot_update_fn=(hot_update if cfg.maa.hot_update_before_catalog else None))
    task_dir = os.path.join(cfg.maa.config_dir, "tasks")
    lock = threading.Lock()
    log.info("监听中，允许触发者 open_id=%s，任务目录=%s", allowed, task_dir)
    for msg in listen(cfg, allowed, max_age_s=cfg.runtime.max_msg_age_s):
        handle_message(msg, router, cfg, lock, llm, identity, task_dir)

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_main.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 全量测试 + Commit**

Run: `.venv/Scripts/python -m pytest -v`
Expected: 全部 PASS（截至本任务共 57 项）
```bash
git add maa_remote/__main__.py tests/test_main.py
git commit -m "feat: main loop with worker-thread execution and single-flight lock"
```

---

### Task 10: evals 回归跑分器

**Files:**
- Create: `maa_remote/eval_router.py`
- Test: `tests/test_eval_router.py`

**Interfaces:**
- Consumes: `LLMClient`/`LLMError`（Task 3）、`load_config`（Task 1）、`evals/router_cases.jsonl`、`schemas/task_plan.schema.json`、`prompts/router.system.md`。
- Produces:
  - `subset_match(expected, actual) -> bool`（部分匹配：expected 出现的字段必须相等，`note` 永不比对，dict 递归）
  - `run_case(llm, system_prompt: str, schema: dict, case: dict) -> tuple[bool, str]`
  - `main(argv=None) -> int`（`python -m maa_remote.eval_router [--cases PATH] [--config PATH]`；全过返回 0，有失败返回 1）

- [ ] **Step 1: 写失败测试**

`tests/test_eval_router.py`:
```python
import json
from maa_remote.eval_router import subset_match, run_case

SCHEMA = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))

def test_subset_match_only_checks_present_fields():
    assert subset_match({"action": "run"}, {"action": "run", "startup": True})
    assert not subset_match({"action": "run"}, {"action": "reject"})

def test_subset_match_recurses_and_skips_note():
    exp = {"action": "run", "fight": {"enable": True, "stone": 0}, "note": "whatever"}
    act = {"action": "run", "fight": {"enable": True, "stone": 0, "stage": ""}, "note": "别的"}
    assert subset_match(exp, act)
    act_bad = {"action": "run", "fight": {"enable": True, "stone": 50}}
    assert not subset_match(exp, act_bad)

def test_run_case_pass():
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps({"action": "run", "startup": True,
                               "fight": {"enable": True, "stage": "CE-6", "times": 3}})
    ok, why = run_case(LLM(), "SYS", SCHEMA,
                       {"input": "打CE-6三次", "expected": {"action": "run", "fight": {"stage": "CE-6"}}})
    assert ok, why

def test_run_case_fails_on_schema_violation():
    class LLM:
        def chat(self, system, user, json_mode=False):
            return json.dumps({"action": "no_such_action"})
    ok, why = run_case(LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}})
    assert not ok and "schema" in why

def test_run_case_fails_on_bad_json():
    class LLM:
        def chat(self, system, user, json_mode=False): return "not json"
    ok, why = run_case(LLM(), "SYS", SCHEMA, {"input": "x", "expected": {"action": "run"}})
    assert not ok
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_eval_router.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 eval_router.py**

`maa_remote/eval_router.py`:
```python
"""意图识别回归：python -m maa_remote.eval_router [--cases PATH] [--config PATH]

逐条把 evals/router_cases.jsonl 喂给 DeepSeek，按「部分匹配」规则比对
（见 evals/README.md）。需要环境变量 DEEPSEEK_API_KEY。
"""
from __future__ import annotations
import argparse
import json
import sys
from jsonschema import validate, ValidationError
from maa_remote.config import load_config
from maa_remote.llm import LLMClient, LLMError

def subset_match(expected, actual) -> bool:
    """expected 里出现的字段必须匹配；note 永不比对；dict 递归。"""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for k, v in expected.items():
            if k == "note":
                continue
            if k not in actual or not subset_match(v, actual[k]):
                return False
        return True
    return expected == actual

def run_case(llm, system_prompt: str, schema: dict, case: dict) -> tuple[bool, str]:
    try:
        raw = llm.chat(system_prompt, case["input"], json_mode=True)
    except LLMError as e:
        return False, f"LLM 调用失败: {e}"
    try:
        actual = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"非法 JSON: {raw[:120]}"
    try:
        validate(actual, schema)
    except ValidationError as e:
        return False, f"schema 不过: {e.message}"
    if not subset_match(case["expected"], actual):
        return False, f"字段不匹配，实际: {json.dumps(actual, ensure_ascii=False)}"
    return True, ""

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", default="evals/router_cases.jsonl")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if not cfg.llm.api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        return 2
    llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model, cfg.llm.request_timeout_s)
    schema = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))
    system_prompt = open("prompts/router.system.md", encoding="utf-8").read()
    with open(args.cases, encoding="utf-8") as f:
        cases = [json.loads(ln) for ln in f if ln.strip()]
    ok_n = 0
    for i, case in enumerate(cases, 1):
        ok, why = run_case(llm, system_prompt, schema, case)
        ok_n += ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {i:02d} {case['input']}" + ("" if ok else f"\n       {why}"))
    print(f"\n{ok_n}/{len(cases)} passed")
    return 0 if ok_n == len(cases) else 1

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_eval_router.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add maa_remote/eval_router.py tests/test_eval_router.py
git commit -m "feat: intent-routing regression runner with subset matching"
```

---

### Task 11: 运维入口（start.bat）

**Files:**
- Create: `start.bat`

**Interfaces:**
- Consumes: `.venv`（Task 1）、`maa_remote/__main__.py`（Task 9）。
- Produces: 双击可启动的入口；README（已存在）的自启说明依赖它。

- [ ] **Step 1: 创建 start.bat**

`start.bat`（**注意存为 GBK/ANSI 或纯 ASCII 注释均可，内容如下**；`cd /d "%~dp0"` 保证从任意工作目录启动都正确）:
```bat
@echo off
rem MAA_remote launcher - double-click to run, or register with Task Scheduler (see README.md)
cd /d "%~dp0"
if "%DEEPSEEK_API_KEY%"=="" (
    echo [ERROR] DEEPSEEK_API_KEY is not set. Configure it in System Environment Variables first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m maa_remote
echo.
echo [maa_remote] process exited unexpectedly - check logs\maa_remote.log
pause
```

- [ ] **Step 2: 语法冒烟**

Run: `cmd /c "echo test-ok"`（确认 cmd 可用）；然后在**未设 DEEPSEEK_API_KEY 的子环境**验证提示分支：
```powershell
cmd /c "set DEEPSEEK_API_KEY=&& echo n | start.bat" 
```
Expected: 输出含 `[ERROR] DEEPSEEK_API_KEY is not set`（不会进入 python）。若本机已设 key，跳过此验证，Task 12 全链路会覆盖。

- [ ] **Step 3: Commit**

```bash
git add start.bat
git commit -m "feat: windows launcher script with env guard"
```

---

### Task 12: 端到端落地校验（真实环境冒烟，不写单测）

> 本任务不写新代码，是执行前的真实环境校验清单。逐项确认，任一失败回到对应模块修正（修正要补/改对应单测再 commit）。

**Files:** 无（可能微调 `maa_remote/listener.py` 的 `parse_event`、`maa_remote/__main__.py` 的 `_auth_status`、`maa_remote/executor.py` 的 task params 或 `config.toml`）。

- [ ] **Step 1: 前置就绪**
  - `echo %DEEPSEEK_API_KEY%` 非空。
  - `lark-cli auth status` 人工执行一次：核对输出**是否 JSON**、bot-only 态（user token 过期）下**有无 `userOpenId`**。
    - 若非 JSON 或缺字段：按真实格式修 `_auth_status()`，或在 `config.toml` 的 `[lark].allowed_sender_open_id` 显式填 open_id（SPEC §0 有本机值）。
  - 设 `MAA_CORE_DIR` 指向 GUI 目录后 `maa version` 不报错（验证复用 GUI core 可行；报错则 `maa install` 装独立 core，并把 `config.toml` 的 `core_dir` 置空）。

- [ ] **Step 2: maa 任务文件离线校验**
  - 手动构造 daily plan 跑 `build_task_file`，写出 json 到 `%APPDATA%/loong/maa/config/tasks/smoke.json`：
    ```powershell
    .venv\Scripts\python -c "import json,os; from maa_remote.models import TaskPlan; from maa_remote.config import load_config, FightConfig; cfg=load_config('config.toml'); from maa_remote.executor import build_task_file; p=TaskPlan.daily(cfg.maa.fight,cfg.maa.daily_tasks); d=os.path.join(cfg.maa.config_dir,'tasks'); os.makedirs(d,exist_ok=True); json.dump(build_task_file(p,cfg.maa.client),open(os.path.join(d,'smoke.json'),'w',encoding='utf-8'),ensure_ascii=False,indent=2); print('written to',d)"
    ```
  - Run: `maa run smoke -a 127.0.0.1:16384 --dry-run`
  - Expected: 解析通过无报错。若某子任务 params 报错，按报错微调 `build_task_file` 对应 params（并同步改 `tests/test_executor.py` 断言）。

- [ ] **Step 3: 模拟器链路**
  - 先手动关掉 MuMu，然后：
    ```powershell
    .venv\Scripts\python -c "from maa_remote.config import load_config; from maa_remote.executor import ensure_emulator; ensure_emulator(load_config('config.toml')); print('emulator ready')"
    ```
  - Expected: MuMu 被拉起，`emulator ready` 在 `boot_timeout_s` 内打印。

- [ ] **Step 4: 飞书事件字段核对**
  - Run: `lark-cli event consume im.message.receive_v1 --as bot --format ndjson`，DM 机器人发"跑日常"。
  - 用真实 NDJSON 核对 `parse_event` 字段路径（`event.message.content` / `chat_id` / `sender.sender_id.open_id` / `create_time`）与 `--format ndjson` 参数本身是否有效。不一致则修 `parse_event`/`listen` 并补对应单测。

- [ ] **Step 5: 回复链路**
  - Run: `lark-cli im +messages-send --chat-id <你的P2P chat_id> --text "冒烟测试" --as bot --json` 实发一条，确认飞书收到（含中文与 emoji 不乱码）。

- [ ] **Step 6: 全链路 + 行为验收**
  - `python -m maa_remote` 启动，逐项验证：
    1. DM"跑日常" → 收到 ack → 模拟器拉起 → maa 跑 → 收到自然语言汇报。
    2. **执行中**再发一句"跑日常" → **立刻**收到 `busy_reply`（验证 worker 线程模型）。
    3. 发"碎 1 颗石头刷 1-7" → 收到确认问句 → 回"取消" → 收到已取消（**不执行**）。
    4. 停掉服务，DM 发一条消息，等 6 分钟后重启服务 → 该消息**不被执行**（验证新鲜度过滤），日志有"忽略过旧消息"。
    5. 检查 `logs/maa_remote.log` 有结构化日志且中文正常。
  - Commit（如有微调）:
  ```bash
  git add -A && git commit -m "fix: align listener/auth/task params with real environment (landing)"
  ```

---

## 落地待采集清单（执行 Task 12 前准备）

1. `DEEPSEEK_API_KEY` 环境变量（系统级）。
2. maa-cli 的 MaaCore：确认复用 GUI core（`MAA_CORE_DIR`）或 `maa install`。
3. 你的 P2P chat_id（用于 Step 5 的 send 冒烟；reply 用 message_id 不需要）。
4. 飞书后台已开 `im.message.receive_v1` 长连接订阅 + bot IM 收发权限。

## Self-Review 记录（计划作者自查，执行者可跳过）

- 评审 6 个必修点全部落入：①引号路径+shlex（Task 7 + config）②worker 线程/busy/新鲜度（Task 6/9）③startup 恒 true（Task 5 + prompt/schema）④MAA_CORE_DIR 注入（Task 7）⑤allowed_sender fail-fast（Task 1/9）⑥UTF-8（Task 1 procutil，全模块）。
- 建议项落入：listener 退避重启（Task 6）、adb connect 轮询（Task 7）、evals 跑分器（Task 10）、碎石/囤药确认（Task 5）、保留 summary（Task 7）、logging+运维（Task 9/11 + README）、daily_tasks 含 fight（Task 2 + config）、不再 git init/覆盖 .gitignore（Task 1）。
- 类型/签名一致性：`runner` 统一 `(cmd, **kw) -> 有 .stdout/.stderr/.returncode 的对象`，默认 `run_utf8`；`execute(plan, cfg, task_dir, runner=..., sleep=..., monotonic=...)` 与 `handle_message` 的 `execute_fn` 调用 `execute_fn(rr.plan, cfg, task_dir, runner=runner)` 兼容（多余的 sleep/monotonic 用默认值）。
