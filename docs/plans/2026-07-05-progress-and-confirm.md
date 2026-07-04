# 进度推送 + 计划确认 Implementation Plan(2026-07-05)

> **For agentic workers(Codex 等):** 按 Task 顺序逐个执行(依赖关系见下)。每个 Step 都要**真的运行命令并核对预期输出**(TDD:先跑失败测试,再实现,再跑通过)。完成一个 Task 就 commit 一次。Step 用 checkbox(`- [ ]`)跟踪。设计依据:`docs/superpowers/specs/2026-07-05-progress-and-confirm-design.md`(冲突时以 spec 为准并停下报告)。**Claude 负责逐任务 review**。
>
> ⚠️ Task 0 需要真机跑一次日常(15~40 分钟,占用模拟器);Task 9 需要用户配合人工验收。其余任务纯本地 TDD。

**Goal:** 跑日常前先在飞书发计划预告、用户确认后执行;执行中把每个模块(公招/基建/作战…)的进度推送到飞书话题;主聊天只保留「开始」和「最终总结」两条。

**Architecture:** 在现有 Listener → Router → Executor → Reporter 流水线上做增量:Router 的花费确认状态机推广为全量确认(预告文案与 `build_task_file` 同源);Executor 从阻塞式改为流式逐行读 maa-cli 输出(或 tail MaaCore asst.log),解析出 taskchain 级进度事件;新增 ProgressSender 把事件合并成「✅ X完成 → Y中…」推到锚点消息的话题里。

**Tech Stack:** 与现状一致(Python 3.14 stdlib + httpx + jsonschema + pytest;maa-cli v0.7.5;lark-cli 1.0.63)。**零新增依赖**。

## Global Constraints

- 沿用 `docs/plans/2026-07-04-maa-remote.md` 的全部红线:零硬编码身份/路径、单飞锁 + worker 线程、旧消息不执行、StartUp 恒开、DeepSeek key 只走环境变量。
- **UTF-8 红线**:所有 `subprocess.Popen` 显式 `text=True, encoding="utf-8", errors="replace"`;`subprocess.run` 走 `procutil.run_utf8`。
- **省钱红线(加强)**:`stone>0` 或 `medicine>0` 的计划**无条件确认**——不受 `confirm.mode`、不受「直接」跳过词影响。
- **进度是锦上添花**:进度解析/推送的任何异常都只记日志,**绝不中断或搞挂执行**。
- **预告与执行同源**:预告文案必须从 `executor.build_task_file()` 的产物渲染,禁止另写一套"描述计划"的逻辑。
- 禁改:`CONTEXT.md`、`SPEC.md`、`prompts/`、`schemas/`、`evals/`。**允许改**:`config.example.toml`(新增节)、本机 `config.toml`(未被 git 跟踪)。
- 测试命令统一:`.venv/Scripts/python -m pytest <file> -v`;每个 Task 结束跑全量 `.venv/Scripts/python -m pytest tests -q` 确认无回归再 commit。
- 依赖顺序:Task 0 → Task 2(fixture);Task 1 → Task 5/7(config);Task 2 → Task 4/6;Task 3 → Task 4;Task 5 → Task 7;全部 → Task 8 → Task 9。Task 0 若暂时没法占用模拟器,可先做 Task 1/3/5。

---

### Task 0: 抓取真实 maa-cli 输出样本(真机,一次日常)

**Files:**
- Create: `scripts/capture_maa_output.py`
- Create: `tests/fixtures/maa_stdout_sample.txt`(抓取产物,人工截取)
- Create: `tests/fixtures/maa_progress_notes.md`(信号源结论)

**Interfaces:**
- Produces: fixture 文件,Task 2 的解析测试以它为准;`maa_progress_notes.md` 记录进度信号源结论(stdout 直读 or asst.log tail),决定 Task 9 里 `config.toml` 是否要填 `asst_log_path`。

- [ ] **Step 1: 写抓取脚本**

`scripts/capture_maa_output.py`:
```python
"""实跑一次日常,抓 maa-cli 完整输出样本,用于锁定进度解析规则。

用法: .venv/Scripts/python scripts/capture_maa_output.py
可选: 先 set MAA_LOG=debug 再跑,获得更详细输出。
"""
import json
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from maa_remote.config import load_config
from maa_remote.executor import build_task_file, ensure_emulator
from maa_remote.models import TaskPlan


def main() -> None:
    cfg = load_config("config.toml")
    ensure_emulator(cfg)

    task_dir = os.path.join(cfg.maa.config_dir, "tasks")
    os.makedirs(task_dir, exist_ok=True)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    name = f"capture_{uuid.uuid4().hex[:8]}"
    with open(os.path.join(task_dir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(build_task_file(plan, cfg.maa.client), f, ensure_ascii=False, indent=2)

    env = dict(os.environ)
    env["MAA_CONFIG_DIR"] = os.path.dirname(task_dir)
    if cfg.maa.core_dir:
        env["MAA_CORE_DIR"] = cfg.maa.core_dir
    if cfg.maa.resource_dir:
        env["MAA_RESOURCE_DIR"] = cfg.maa.resource_dir
    adb_dir = os.path.dirname(cfg.emulator.adb_path)
    if adb_dir:
        env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")

    cmd = [cfg.maa.maa_cli_path, "run", name, "-a", cfg.emulator.adb_serial, "--batch"]
    out_path = "logs/maa_stdout_capture.txt"
    os.makedirs("logs", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        for line in proc.stdout:
            out.write(line)
            out.flush()
            print(line, end="")
        code = proc.wait()
    print(f"\nexit={code}, saved to {out_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 跑一次(占用模拟器,15~40 分钟)**

Run: `.venv/Scripts/python scripts/capture_maa_output.py`
Expected: 控制台实时滚动 maa 输出,结束打印 `exit=0, saved to logs/maa_stdout_capture.txt`。(当天日常已清过也没关系——各 taskchain 仍会开始/完成,信号照样有。)

- [ ] **Step 3: 判定信号源**

Run: `grep -c "TaskChain" logs/maa_stdout_capture.txt` 以及 `grep -inE "startup|recruit|infrast|mall|award|fight" logs/maa_stdout_capture.txt | head -30`

判定规则(按顺序取第一个满足的):
1. stdout 里有 `TaskChainStart/Completed`(或每个模块名有清晰的开始/完成行)→ **信号源 = stdout**,记录样例行。
2. 没有 → `set MAA_LOG=debug` 重跑 Step 2 一次;有了 → 信号源 = stdout(需在 Task 8 给服务进程设 `MAA_LOG`,把这点写进 notes)。
3. 还没有 → 找 MaaCore 日志:依次检查 `%APPDATA%\loong\maa\debug\asst.log`、`%APPDATA%\loong\maa\data\debug\asst.log`、`<core_dir>\debug\asst.log`,取**本次运行时间戳**匹配且含 `TaskChainStart` 的那个 → **信号源 = asst.log tail**,记录完整路径。

- [ ] **Step 4: 落 fixture 与结论**

把含所有 taskchain 开始/完成事件的行(外加开头/结尾各 ~30 行上下文)从信号源文件截取到 `tests/fixtures/maa_stdout_sample.txt`(≤200KB,删掉与进度无关的海量重复行)。写 `tests/fixtures/maa_progress_notes.md`:信号源结论、每种事件的原始样例行各一条、asst.log 路径(如适用)、是否需要 `MAA_LOG` 环境变量。

- [ ] **Step 5: Commit**

```bash
git add scripts/capture_maa_output.py tests/fixtures/maa_stdout_sample.txt tests/fixtures/maa_progress_notes.md
git commit -m "chore: capture real maa-cli output fixture for progress parsing"
```

---

### Task 1: 配置扩展 [progress] / [confirm] / asst_log_path

**Files:**
- Modify: `maa_remote/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_config.py`(追加)

**Interfaces:**
- Produces:
  - `ProgressConfig(enable: bool, style: str)`;`ConfirmConfig(mode: str, ttl_s: int)`
  - `Config` 新增字段 `.progress: ProgressConfig`、`.confirm: ConfirmConfig`
  - `MaaConfig` 新增字段 `.asst_log_path: str`(默认 `""` = 不用 tailer)
  - **节缺失时的默认值**:`progress.enable=True, style="thread"`;`confirm.mode="always", ttl_s=600`;`asst_log_path=""`——老配置文件不炸(tests/test_executor.py 的内联 `_CONFIG` 就没有这些节,必须继续能加载)。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_config.py`)

用 `config.example.toml` 作为"不含新节的老配置"基底(Step 3 之后 example 会加新节,所以这里用**删掉新节**的副本;基底与覆盖都自包含,不依赖文件里其他 helper):

```python
import re


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
    body = open(path, encoding="utf-8").read().replace(
        "[maa]", '[maa]\nasst_log_path = "%APPDATA%/loong/maa/debug/asst.log"', 1
    )
    open(path, "w", encoding="utf-8").write(body)
    cfg2 = load_config(path, env=_ENV)
    assert cfg2.maa.asst_log_path == "y/loong/maa/debug/asst.log"


def test_progress_and_confirm_sections_override(tmp_path):
    path = _example_without(tmp_path, "progress", "confirm")
    with open(path, "a", encoding="utf-8") as f:
        f.write('\n[progress]\nenable = false\nstyle = "flat"\n[confirm]\nmode = "spend_only"\nttl_s = 120\n')
    cfg = load_config(path, env=_ENV)
    assert cfg.progress.enable is False
    assert cfg.progress.style == "flat"
    assert cfg.confirm.mode == "spend_only"
    assert cfg.confirm.ttl_s == 120
```
(注意 `config.example.toml` 现状还没有 `asst_log_path`,`_example_without` 删节的正则对它是幂等的——Step 3 加上新节后这些测试同样成立。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -v`
Expected: 新用例 FAIL(`Config` 无 `progress` 属性)。

- [ ] **Step 3: 实现**

`maa_remote/config.py` 追加 dataclass 与加载逻辑:
```python
@dataclass
class ProgressConfig:
    enable: bool
    style: str  # "thread" | "flat"


@dataclass
class ConfirmConfig:
    mode: str  # "always" | "spend_only"
    ttl_s: int
```
`Config` 增加 `progress: ProgressConfig`、`confirm: ConfirmConfig` 字段;`MaaConfig` 增加 `asst_log_path: str`。`load_config` 内:
```python
    progress = data.get("progress", {})
    confirm = data.get("confirm", {})
    # MaaConfig(...) 里:
    asst_log_path=_expand(maa.get("asst_log_path", ""), env),
    # Config(...) 里:
    progress=ProgressConfig(
        enable=progress.get("enable", True),
        style=progress.get("style", "thread"),
    ),
    confirm=ConfirmConfig(
        mode=confirm.get("mode", "always"),
        ttl_s=confirm.get("ttl_s", 600),
    ),
```

`config.example.toml` 追加(带注释):
```toml
[progress]
enable = true       # 执行中是否往飞书推进度
style = "thread"    # thread=进度发在开始消息的话题里 | flat=直接分条回复(话题体验不好时的回退)

[confirm]
mode = "always"     # always=每次先预告等确认 | spend_only=只有碎石/囤药计划才确认
ttl_s = 600         # 预告确认的有效期(秒),过期作废
```
并在 `[maa]` 节补 `asst_log_path = ""` + 注释「进度信号备选源:MaaCore asst.log 完整路径;留空=直接解析 maa-cli 输出」。

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS(老配置无新节也能加载)。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/config.py config.example.toml tests/test_config.py
git commit -m "feat: progress/confirm config sections with backward-compatible defaults"
```

---

### Task 2: progress.py —— 事件模型与日志行解析

**Files:**
- Create: `maa_remote/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: Task 0 的 `tests/fixtures/maa_stdout_sample.txt`。
- Produces(Task 4/6 依赖,签名固定):
  - `@dataclass ProgressEvent(phase: str, text: str)`——`phase ∈ {"start","done","error","info"}`,`text` 为已格式化中文片段(如 `"🎫 公招中…"`)。
  - `TASKCHAIN_LABELS: dict[str, tuple[str, str]]`
  - `parse_progress_line(line: str) -> ProgressEvent | None`

- [ ] **Step 1: 写失败测试**

`tests/test_progress.py`:
```python
from maa_remote.progress import ProgressEvent, parse_progress_line

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
    # 日常必含公招:任一事件文本应提到公招(或 fixture 实际包含的等价模块)
    assert any("公招" in e.text for e in events)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_progress.py -v`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 实现**

`maa_remote/progress.py`:
```python
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("maa_remote.progress")


@dataclass
class ProgressEvent:
    phase: str  # "start" | "done" | "error" | "info"
    text: str   # 已格式化的中文片段,如 "🎫 公招中…" / "✅ 公招完成"


TASKCHAIN_LABELS: dict[str, tuple[str, str]] = {
    "StartUp": ("🎮", "启动游戏"),
    "Recruit": ("🎫", "公招"),
    "Infrast": ("🏗️", "基建换班"),
    "Mall": ("🛒", "信用商店"),
    "Award": ("🎁", "领奖励"),
    "Fight": ("⚔️", "刷理智"),
    "CloseDown": ("🚪", "关闭游戏"),
}

_CHAIN_RE = re.compile(r'"taskchain"\s*:\s*"(\w+)"')


def _label(chain: str) -> tuple[str, str]:
    return TASKCHAIN_LABELS.get(chain, ("▶️", chain))


def parse_progress_line(line: str) -> ProgressEvent | None:
    if not line or "SubTask" in line:
        return None
    m = _CHAIN_RE.search(line)
    if m:
        emoji, label = _label(m.group(1))
        if "TaskChainStart" in line:
            return ProgressEvent("start", f"{emoji} {label}中…")
        if "TaskChainCompleted" in line:
            return ProgressEvent("done", f"✅ {label}完成")
        if "TaskChainError" in line:
            return ProgressEvent("error", f"❌ {label}失败")
        if "TaskChainStopped" in line:
            return ProgressEvent("info", f"⏹️ {label}中止")
    return None
```

**⚠️ 以 fixture 为准调整**:若 Task 0 的 notes 表明信号源是 maa-cli 纯文本(不含 `"taskchain":"X"` JSON),在 `parse_progress_line` 末尾按 notes 里的样例行追加第二组正则(命名 `_PLAIN_RE`),映射到同样的四种 phase。`test_real_fixture_yields_ordered_chain_events` 必须通过——它是仲裁者。

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_progress.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/progress.py tests/test_progress.py
git commit -m "feat: taskchain progress event parsing locked by real fixture"
```

---

### Task 3: reporter.send_reply 扩展(返回 message_id + 话题回复)

**Files:**
- Modify: `maa_remote/reporter.py`
- Test: `tests/test_reporter.py`(追加)

**Interfaces:**
- Produces(Task 4/8 依赖):
  - `send_reply(message_id: str, text: str, identity: str, runner=run_utf8, reply_in_thread: bool = False) -> str | None`——返回**新发出消息**的 message_id;解析不到或发送失败返回 `None`,**不抛异常**。
  - 内部 `_extract_message_id(stdout: str) -> str | None`(兼容 `{"message_id":...}` 与 `{"data":{"message_id":...}}` 两种形状)。
- 兼容:现有调用点(`__main__.py`、`reporter.report`)不传新参数,行为不变。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_reporter.py`)

```python
def _runner_with_stdout(stdout, calls):
    class R:
        returncode = 0
        stderr = ""
    R.stdout = stdout

    def runner(cmd, **kw):
        calls.append(cmd)
        return R()

    return runner


def test_send_reply_returns_message_id_from_nested_json():
    calls = []
    mid = send_reply("om_1", "hi", "bot", runner=_runner_with_stdout('{"data":{"message_id":"om_new"}}', calls))
    assert mid == "om_new"


def test_send_reply_returns_top_level_message_id():
    calls = []
    assert send_reply("om_1", "hi", "bot", runner=_runner_with_stdout('{"message_id":"om_x"}', calls)) == "om_x"


def test_send_reply_returns_none_on_unparseable_output():
    calls = []
    assert send_reply("om_1", "hi", "bot", runner=_runner_with_stdout("oops", calls)) is None


def test_send_reply_thread_flag_appends_arg():
    calls = []
    send_reply("om_1", "hi", "bot", runner=_runner_with_stdout("{}", calls), reply_in_thread=True)
    assert "--reply-in-thread" in calls[0]


def test_send_reply_swallows_runner_exception():
    def boom(cmd, **kw):
        raise OSError("lark-cli missing")
    assert send_reply("om_1", "hi", "bot", runner=boom) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_reporter.py -v`
Expected: 新用例 FAIL(返回 None 而非 message_id / 报 TypeError)。

- [ ] **Step 3: 实现**

`maa_remote/reporter.py` 的 `send_reply` 改为:
```python
import json
import logging

log = logging.getLogger("maa_remote.reporter")


def _extract_message_id(stdout: str) -> str | None:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("message_id"):
        return data["message_id"]
    inner = data.get("data")
    if isinstance(inner, dict) and inner.get("message_id"):
        return inner["message_id"]
    return None


def send_reply(
    message_id: str,
    text: str,
    identity: str,
    runner=run_utf8,
    reply_in_thread: bool = False,
) -> str | None:
    cmd = [
        resolve_executable("lark-cli"),
        "im", "+messages-reply",
        "--message-id", message_id,
        "--text", text,
        "--as", identity,
        "--json",
    ]
    if reply_in_thread:
        cmd.append("--reply-in-thread")
    try:
        result = runner(cmd, timeout=30)
    except Exception:
        log.exception("lark 回复发送失败(忽略)")
        return None
    return _extract_message_id(getattr(result, "stdout", "") or "")
```

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_reporter.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS(老用例 `test_send_reply_invokes_lark_cli` 不需要改)。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/reporter.py tests/test_reporter.py
git commit -m "feat: send_reply returns message id and supports thread replies"
```

---

### Task 4: progress.py —— ProgressSender(合并推送 + 永不抛异常)

**Files:**
- Modify: `maa_remote/progress.py`(追加)
- Test: `tests/test_progress.py`(追加)

**Interfaces:**
- Consumes: Task 2 的 `ProgressEvent`;Task 3 的 `send_reply`。
- Produces(Task 8 依赖):
  - `ProgressSender(anchor_message_id: str | None, trigger_message_id: str, identity: str, style: str = "thread", runner=run_utf8)`
  - `.handle(event: ProgressEvent) -> None`(**绝不抛异常**;作为 executor 的 `on_event` 回调)
  - `.flush() -> None`(冲刷缓冲的「✅ X完成」;执行结束后调用)
  - 行为:`done` 事件先缓冲,遇到下一个 `start` 合并成 `"✅ X完成 → 🎫 Y中…"` 一条发出;`error` 先冲刷再立发;`info` 直发。`anchor_message_id 为 None` 时自动降级 `flat`(普通回复触发消息)。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_progress.py`)

```python
from maa_remote.progress import ProgressSender


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
    assert len(calls) == 1  # 幂等


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
    s.handle(ProgressEvent("start", "🎫 公招中…"))  # 不应抛
    s.handle(ProgressEvent("done", "✅ 公招完成"))
    s.flush()  # 不应抛
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_progress.py -v`
Expected: 新用例 FAIL(`ProgressSender` 不存在)。

- [ ] **Step 3: 实现**(追加到 `maa_remote/progress.py`)

```python
from maa_remote.procutil import run_utf8
from maa_remote.reporter import send_reply


class ProgressSender:
    """把进度事件合并成简洁的飞书消息。任何异常只记日志,绝不影响执行。"""

    def __init__(
        self,
        anchor_message_id: str | None,
        trigger_message_id: str,
        identity: str,
        style: str = "thread",
        runner=run_utf8,
    ):
        self.anchor = anchor_message_id
        self.trigger = trigger_message_id
        self.identity = identity
        self.style = style if anchor_message_id else "flat"
        self.runner = runner
        self._pending_done: str | None = None

    def handle(self, event: ProgressEvent) -> None:
        try:
            if event.phase == "start":
                text = f"{self._pending_done} → {event.text}" if self._pending_done else event.text
                self._pending_done = None
                self._send(text)
            elif event.phase == "done":
                if self._pending_done:
                    self._send(self._pending_done)
                self._pending_done = event.text
            elif event.phase == "error":
                self.flush()
                self._send(event.text)
            else:
                self._send(event.text)
        except Exception:
            log.exception("进度推送失败(不影响执行)")

    def flush(self) -> None:
        try:
            if self._pending_done:
                self._send(self._pending_done)
                self._pending_done = None
        except Exception:
            log.exception("进度冲刷失败(不影响执行)")

    def _send(self, text: str) -> None:
        if self.style == "thread":
            send_reply(self.anchor, text, self.identity, runner=self.runner, reply_in_thread=True)
        else:
            send_reply(self.trigger, text, self.identity, runner=self.runner)
```
注意:`send_reply` 自身已吞异常返回 None(Task 3),这里的 try/except 是第二道保险(比如未来有人改了 send_reply)。

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_progress.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/progress.py tests/test_progress.py
git commit -m "feat: progress sender with merge buffering and thread/flat styles"
```

---

### Task 5: preview.py —— 计划预告(与 build_task_file 同源)

**Files:**
- Create: `maa_remote/preview.py`
- Test: `tests/test_preview.py`

**Interfaces:**
- Consumes: `executor.build_task_file(plan, client) -> {"tasks": [...]}`;`cfg.confirm.ttl_s`(Task 1)。
- Produces(Task 7 依赖):`plan_preview(plan: TaskPlan, cfg: Config) -> str`。

- [ ] **Step 1: 写失败测试**

`tests/test_preview.py`:
```python
import shutil

from maa_remote.config import load_config
from maa_remote.models import Fight, TaskPlan
from maa_remote.preview import plan_preview


def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(
        str(tmp_path / "config.toml"),
        env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"},
    )


def test_daily_preview_lists_all_modules_and_footer(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    text = plan_preview(plan, cfg)
    for kw in ["📋", "公招", "基建", "信用商店", "奖励", "只吃快过期的药", "不动囤药", "不碎石", "取消"]:
        assert kw in text, kw
    assert "10 分钟" in text  # ttl_s=600


def test_spend_plan_preview_shows_warnings(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True, stage="UR-8", medicine=2, stone=5))
    plan.recruit.enable = False
    plan.infrast.enable = False
    plan.mall.enable = False
    plan.award.enable = False
    text = plan_preview(plan, cfg)
    assert "⚠️ 动用 2 瓶囤积理智药" in text
    assert "⚠️ 碎 5 颗源石" in text
    assert "UR-8" in text
    assert "公招" not in text  # 关闭的模块不出现


def test_fight_times_and_default_stage_wording(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan(action="run", fight=Fight(enable=True, stage="", times=3))
    text = plan_preview(plan, cfg)
    assert "最多 3 次" in text
    assert "上次/当前关卡" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_preview.py -v`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 实现**

`maa_remote/preview.py`:
```python
from __future__ import annotations

from maa_remote.config import Config
from maa_remote.executor import build_task_file
from maa_remote.models import TaskPlan

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _num(i: int) -> str:
    return _CIRCLED[i - 1] if 1 <= i <= len(_CIRCLED) else f"{i}."


def _describe(task: dict) -> str:
    t = task["type"]
    p = task.get("params", {})
    if t == "StartUp":
        return "开游戏(已在游戏内则秒过)"
    if t == "Recruit":
        return f"公招:最多 {p.get('times', 4)} 次(自动选高星词条,不加急)"
    if t == "Infrast":
        return "基建:游戏内一键换班"
    if t == "Mall":
        buy = "、".join(p.get("buy_first", []))
        skip = "、".join(p.get("blacklist", []))
        return f"信用商店:优先买{buy}(不买{skip})"
    if t == "Award":
        return "领日常任务奖励 & 收邮件"
    if t == "Fight":
        stage = p.get("stage") or "上次/当前关卡"
        parts = [f"刷理智:{stage}"]
        if p.get("times") is not None:
            parts.append(f"最多 {p['times']} 次")
        parts.append("只吃快过期的药" if p.get("expiring_medicine") else "不吃过期药")
        medicine = p.get("medicine", 0)
        stone = p.get("stone", 0)
        parts.append(f"⚠️ 动用 {medicine} 瓶囤积理智药" if medicine > 0 else "不动囤药")
        parts.append(f"⚠️ 碎 {stone} 颗源石" if stone > 0 else "不碎石")
        return ",".join(parts)
    return t


def plan_preview(plan: TaskPlan, cfg: Config) -> str:
    tasks = build_task_file(plan, cfg.maa.client)["tasks"]
    lines = ["📋 本次计划"]
    for i, task in enumerate(tasks, 1):
        lines.append(f"{_num(i)} {_describe(task)}")
    ttl_min = max(1, cfg.confirm.ttl_s // 60)
    lines.append(f"回「1」或「确认」开始;回「取消」作废;{ttl_min} 分钟不回自动作废。")
    return "\n".join(lines)
```

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_preview.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/preview.py tests/test_preview.py
git commit -m "feat: plan preview rendered from build_task_file output"
```

---

### Task 6: executor 流式改造(逐行读 + 看门狗 + asst.log tailer)

**Files:**
- Modify: `maa_remote/executor.py`
- Create: `tests/conftest.py`(FakePopen)
- Test: `tests/test_executor.py`(改 3 个旧 run_maa 用例 + 追加)

**Interfaces:**
- Consumes: Task 2 的 `parse_progress_line` / `ProgressEvent`。
- Produces(Task 8 依赖,签名固定):
  - `run_maa(plan, cfg, task_dir, popen=subprocess.Popen, on_event=None) -> ExecResult`(**去掉了 runner 参数**,maa 子进程改走 popen)
  - `ensure_emulator(cfg, runner=run_utf8, sleep=..., monotonic=..., on_event=None) -> None`(开始时发 `ProgressEvent("start","🖥️ 拉起模拟器中…")`,就绪发 `ProgressEvent("done","✅ 模拟器就绪")`)
  - `execute(plan, cfg, task_dir, runner=run_utf8, sleep=..., monotonic=..., on_event=None, popen=subprocess.Popen) -> ExecResult`
  - `AsstLogTailer(log_path: str, on_event, poll_interval_s: float = 1.0)`(上下文管理器;进入时记住文件末尾,后台线程只读**新增**行喂 `parse_progress_line`)
  - 行为:`cfg.maa.asst_log_path` 非空且 `on_event` 非 None 时启用 tailer 并**跳过** stdout 行解析(防双份事件);超时由 `threading.Timer` 看门狗 kill,返回 `error` 含「超时」;`on_event` 非 None 但整个 maa 阶段**一个进度事件都没解析到**时,结束前补发一条 `ProgressEvent("info", "ℹ️ 本次没拿到细粒度进度，请等最终总结")`(spec §五「解析不到进度信号」的兜底)。

- [ ] **Step 1: 建 FakePopen**

`tests/conftest.py`:
```python
class FakePopen:
    """脚本化 Popen 替身。实例本身可作为 popen 工厂传入:popen=FakePopen([...])"""

    def __init__(self, lines, returncode=0, boom=None):
        self._lines = lines
        self._returncode = returncode
        self._boom = boom
        self.killed = False
        self.cmd = None
        self.kw = None

    def __call__(self, cmd, **kw):
        if self._boom is not None:
            raise self._boom
        self.cmd = cmd
        self.kw = kw
        return self

    @property
    def stdout(self):
        return iter(line + "\n" for line in self._lines)

    @property
    def returncode(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self.killed = True
```

- [ ] **Step 2: 写失败测试**

`tests/test_executor.py`:旧的 `test_run_maa_writes_task_and_injects_env` / `test_run_maa_nonzero_is_failure` / `test_run_maa_runner_exception_is_failure` 改成 popen 版;`test_execute_emulator_failure_short_circuits` / `test_execute_closes_emulator_when_configured` 增加 `popen=FakePopen([""])` 入参。文件顶部加 `from conftest import FakePopen` 与 `from maa_remote.progress import ProgressEvent`。

```python
def test_run_maa_writes_task_and_injects_env(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    task_dir = str(tmp_path / "tasks")
    popen = FakePopen(["Summary", "all done"])
    res = run_maa(plan, cfg, task_dir, popen=popen)
    assert res.ok is True
    assert popen.cmd[0] == cfg.maa.maa_cli_path and "--batch" in popen.cmd
    env = popen.kw["env"]
    assert env["MAA_CONFIG_DIR"] == os.path.dirname(task_dir)
    assert env["MAA_CORE_DIR"] == cfg.maa.core_dir
    assert popen.kw["encoding"] == "utf-8" and popen.kw["errors"] == "replace"
    assert "Summary" in res.raw_log


def test_run_maa_nonzero_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(["boom"], returncode=2))
    assert res.ok is False and "退出码 2" in res.error


def test_run_maa_popen_exception_is_failure(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen([], boom=OSError("no exe")))
    assert res.ok is False and "maa 启动失败" in res.error


ASST = 'Assistant::append_callback | TaskChain{kind} {{"taskchain":"{chain}","taskid":1,"uuid":"X"}}'


def test_run_maa_emits_progress_events_in_order(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    lines = [
        ASST.format(kind="Start", chain="StartUp"),
        "noise",
        ASST.format(kind="Completed", chain="StartUp"),
        ASST.format(kind="Start", chain="Recruit"),
        ASST.format(kind="Completed", chain="Recruit"),
    ]
    events = []
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(lines), on_event=events.append)
    assert res.ok is True
    assert [e.phase for e in events] == ["start", "done", "start", "done"]
    assert "公招" in events[2].text


def test_run_maa_on_event_exception_does_not_fail_run(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    lines = [ASST.format(kind="Start", chain="Recruit")]

    def bad_cb(event):
        raise RuntimeError("callback broke")

    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(lines), on_event=bad_cb)
    assert res.ok is True


def test_run_maa_timeout_kills_and_reports(tmp_path):
    import time as _t
    cfg = _cfg(tmp_path)
    cfg.maa.task_timeout_s = 0.05
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)

    class SlowPopen(FakePopen):
        @property
        def stdout(self):
            def gen():
                for line in self._lines:
                    if self.killed:
                        return
                    _t.sleep(0.03)
                    yield line + "\n"
            return gen()

    popen = SlowPopen(["line"] * 50)
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=popen)
    assert res.ok is False and "超时" in res.error
    assert popen.killed is True


def test_ensure_emulator_emits_progress_events(tmp_path):
    cfg = _cfg(tmp_path)
    events = []

    def runner(cmd, **kw):
        return R("device\n")

    ensure_emulator(cfg, runner=runner, sleep=lambda s: None, monotonic=lambda: 0.0, on_event=events.append)
    assert [e.phase for e in events] == ["start", "done"]
    assert "模拟器" in events[0].text and "模拟器" in events[1].text


def test_run_maa_no_signals_emits_fallback_notice(tmp_path):
    cfg = _cfg(tmp_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    events = []
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(["no signal here"]), on_event=events.append)
    assert res.ok is True
    assert [e.phase for e in events] == ["info"]
    assert "细粒度进度" in events[0].text


def test_run_maa_with_asst_log_path_uses_tailer_not_stdout(tmp_path):
    cfg = _cfg(tmp_path)
    log_path = tmp_path / "asst.log"
    log_path.write_text("", encoding="utf-8")
    cfg.maa.asst_log_path = str(log_path)
    plan = TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks)
    # stdout 里有事件行,但启用 tailer 后不应从 stdout 解析(防双份)
    lines = [ASST.format(kind="Start", chain="Recruit")]
    events = []
    res = run_maa(plan, cfg, str(tmp_path / "tasks"), popen=FakePopen(lines), on_event=events.append)
    assert res.ok is True
    # asst.log 是空的、stdout 被跳过 → 零信号,只剩兜底提示
    assert [e.phase for e in events] == ["info"]


def test_asst_log_tailer_reads_only_new_lines(tmp_path):
    import time as _t
    from maa_remote.executor import AsstLogTailer

    log_path = tmp_path / "asst.log"
    log_path.write_text(ASST.format(kind="Start", chain="StartUp") + "\n", encoding="utf-8")
    events = []
    with AsstLogTailer(str(log_path), events.append, poll_interval_s=0.01):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(ASST.format(kind="Start", chain="Recruit") + "\n")
        deadline = _t.monotonic() + 2
        while not events and _t.monotonic() < deadline:
            _t.sleep(0.02)
    assert len(events) == 1  # 只有新增行,进入前的 StartUp 行不算
    assert events[0].phase == "start" and "公招" in events[0].text
```

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_executor.py -v`
Expected: 新/改用例 FAIL(`run_maa` 不接受 popen 参数等)。

- [ ] **Step 4: 实现**

`maa_remote/executor.py`:文件顶部加 `import logging`、`import subprocess`、`import threading`、`from maa_remote.progress import ProgressEvent, parse_progress_line`,以及 `log = logging.getLogger("maa_remote.executor")`。

`ensure_emulator` 增加 `on_event=None` 参数,函数体开头:
```python
    if on_event is not None:
        on_event(ProgressEvent("start", "🖥️ 拉起模拟器中…"))
```
`return` 前(state 为 device 的分支):
```python
            if on_event is not None:
                on_event(ProgressEvent("done", "✅ 模拟器就绪"))
            return
```

新增 `AsstLogTailer`(放在 `run_maa` 之前):
```python
class AsstLogTailer:
    """maa 运行期间 tail MaaCore asst.log,把新增行解析成进度事件。"""

    def __init__(self, log_path: str, on_event, poll_interval_s: float = 1.0):
        self.log_path = log_path
        self.on_event = on_event
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def __enter__(self) -> "AsstLogTailer":
        try:
            self._offset = os.path.getsize(self.log_path)
        except OSError:
            self._offset = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain()
            self._stop.wait(self.poll_interval_s)
        self._drain()

    def _drain(self) -> None:
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                for line in f:
                    event = parse_progress_line(line)
                    if event is not None:
                        try:
                            self.on_event(event)
                        except Exception:
                            log.exception("进度回调失败(忽略)")
                self._offset = f.tell()
        except OSError:
            pass
```

`run_maa` 整体替换为:
```python
def run_maa(
    plan: TaskPlan,
    cfg: Config,
    task_dir: str,
    popen=subprocess.Popen,
    on_event=None,
) -> ExecResult:
    os.makedirs(task_dir, exist_ok=True)
    name = f"maa_remote_{uuid.uuid4().hex[:8]}"
    task_path = os.path.join(task_dir, f"{name}.json")
    with open(task_path, "w", encoding="utf-8") as f:
        json.dump(build_task_file(plan, cfg.maa.client), f, ensure_ascii=False, indent=2)

    cmd = [cfg.maa.maa_cli_path, "run", name, "-a", cfg.emulator.adb_serial, "--batch"]
    env = dict(os.environ)
    env["MAA_CONFIG_DIR"] = os.path.dirname(task_dir)
    if cfg.maa.core_dir:
        env["MAA_CORE_DIR"] = cfg.maa.core_dir
    if cfg.maa.resource_dir:
        env["MAA_RESOURCE_DIR"] = cfg.maa.resource_dir
    adb_dir = os.path.dirname(cfg.emulator.adb_path)
    if adb_dir:
        env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")

    try:
        proc = popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as exc:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=f"maa 启动失败: {exc}")

    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(cfg.maa.task_timeout_s, _kill)
    watchdog.daemon = True
    watchdog.start()

    use_tailer = bool(cfg.maa.asst_log_path) and on_event is not None
    lines: list[str] = []
    delivered = {"n": 0}

    def _emit(event) -> None:
        delivered["n"] += 1
        try:
            on_event(event)
        except Exception:
            log.exception("进度回调失败(忽略)")

    def _pump() -> None:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if on_event is not None and not use_tailer:
                event = parse_progress_line(line)
                if event is not None:
                    _emit(event)

    try:
        if use_tailer:
            with AsstLogTailer(cfg.maa.asst_log_path, _emit):
                _pump()
                returncode = proc.wait()
        else:
            _pump()
            returncode = proc.wait()
    finally:
        watchdog.cancel()

    if on_event is not None and delivered["n"] == 0 and not timed_out.is_set():
        _emit(ProgressEvent("info", "ℹ️ 本次没拿到细粒度进度，请等最终总结"))

    raw_log = "\n".join(lines)
    facts = parse_maa_log(raw_log)
    if timed_out.is_set():
        return ExecResult(
            ok=False, exit_code=-1, raw_log=raw_log, facts=facts,
            error=f"MAA 超时(超过 {cfg.maa.task_timeout_s}s),已强制终止",
        )
    if returncode != 0:
        return ExecResult(
            ok=False, exit_code=returncode, raw_log=raw_log, facts=facts,
            error=f"MAA 非零退出（退出码 {returncode}）",
        )
    return ExecResult(ok=True, exit_code=0, raw_log=raw_log, facts=facts, error=None)
```

`execute` 改为:
```python
def execute(
    plan: TaskPlan,
    cfg: Config,
    task_dir: str,
    runner=run_utf8,
    sleep=time.sleep,
    monotonic=time.monotonic,
    on_event=None,
    popen=subprocess.Popen,
) -> ExecResult:
    try:
        ensure_emulator(cfg, runner=runner, sleep=sleep, monotonic=monotonic, on_event=on_event)
    except EmulatorError as exc:
        return ExecResult(ok=False, exit_code=-1, raw_log="", facts={}, error=str(exc))

    result = run_maa(plan, cfg, task_dir, popen=popen, on_event=on_event)
    if cfg.emulator.close_after:
        runner(shlex.split(cfg.emulator.shutdown_cmd), timeout=60)
    return result
```

- [ ] **Step 5: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_executor.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add maa_remote/executor.py tests/test_executor.py tests/conftest.py
git commit -m "feat: streaming maa execution with progress events, watchdog and asst.log tailer"
```

---

### Task 7: router 确认状态机推广(全量确认 + 跳过词 + 新指令覆盖)

**Files:**
- Modify: `maa_remote/router.py`
- Test: `tests/test_router.py`(改旧用例 + 追加)

**Interfaces:**
- Consumes: Task 1 的 `cfg.confirm`;Task 5 的 `plan_preview`。
- Produces: `Router.route` 行为——
  1. 花费计划(stone>0 或 medicine>0)**无条件**先确认;
  2. `confirm.mode=="always"` 时其余计划也先确认(预告 = `plan_preview`,花费计划在预告前加 ⚠️ 头行);
  3. 「直接」前缀 + FAST_PATH(如「直接跑日常」)跳过第 2 条(但跳不过第 1 条);
  4. 待确认期间收到非确认/取消词 → 作废旧计划,当新指令完整重路由;
  5. 确认词集合扩为 `{"确认","确定","是","yes","y","1","开始"}`;确认 TTL 用 `cfg.confirm.ttl_s`。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_router.py`)

```python
def test_fast_path_daily_previews_then_confirm_executes(tmp_path):
    llm = FakeLLM("SHOULD_NOT_BE_CALLED")
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = router.route(_msg("跑日常"))
    assert rr.kind == "reply" and "📋" in rr.reply and "基建" in rr.reply
    rr2 = router.route(_msg("1"))
    assert rr2.kind == "execute" and rr2.plan.recruit.enable is True
    assert llm.calls == []


def test_skip_prefix_bypasses_confirm(tmp_path):
    router = Router(_cfg(tmp_path), FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA)
    rr = router.route(_msg("直接跑日常"))
    assert rr.kind == "execute" and rr.plan.recruit.enable is True


def test_skip_prefix_cannot_bypass_spend_confirmation(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.maa.fight.medicine = 999  # 让默认日常变成"花钱"计划
    router = Router(cfg, FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA)
    rr = router.route(_msg("直接跑日常"))
    assert rr.kind == "reply" and "⚠️" in rr.reply


def test_new_command_replaces_pending_confirm(tmp_path):
    llm = FakeLLM(json.dumps({"action": "run", "fight": {"enable": True, "stage": "CE-6"}}))
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr2 = router.route(_msg("刷CE-6"))
    assert rr2.kind == "reply" and "CE-6" in rr2.reply  # 新计划的预告顶掉旧的
    rr3 = router.route(_msg("确认"))
    assert rr3.kind == "execute" and rr3.plan.fight.stage == "CE-6"


def test_confirm_mode_spend_only_executes_daily_directly(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(cfg, FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "execute"


def test_confirm_uses_confirm_ttl(tmp_path):
    clock = {"v": 0.0}
    cfg = _cfg(tmp_path)  # ttl_s = 600
    router = Router(cfg, FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA, now_fn=lambda: clock["v"])
    assert router.route(_msg("跑日常")).kind == "reply"
    clock["v"] = 599.0
    assert router.route(_msg("1")).kind == "execute"  # 未过期
```

- [ ] **Step 2: 改受影响的旧用例**(同文件)

| 旧用例 | 改法 |
|---|---|
| `test_fast_path_daily_bypasses_llm` | 删除(被 `test_fast_path_daily_previews_then_confirm_executes` 取代) |
| `test_llm_path_specific_stage` | 先 `cfg = _cfg(tmp_path); cfg.confirm.mode = "spend_only"`,再 `Router(cfg, ...)`(该用例测的是 LLM 计划映射,不测确认) |
| `test_ask_stage_selection_then_pick_executes_with_startup` | 同上加 `cfg.confirm.mode = "spend_only"` |
| `test_invalid_json_then_retry_success` | 同上 |
| `test_schema_violation_then_retry_success` | 同上 |
| `test_confirmation_expires_by_ttl` | `clock["v"] = 301.0` 改为 `601.0`(TTL 现在是 confirm.ttl_s=600);过期后回「确认」走全新路由 → FakeLLM 返回花费计划 → 仍是 `reply`,断言不变 |
| 其余(stone/medicine 确认、cancel、优先级)| 不改——花费确认路径保留,预告文案含「碎 50 颗源石」「确认」等关键词,原断言仍成立;若个别断言失败,以「预告文案 = ⚠️ 头行 + plan_preview」核对关键词后修正断言字符串 |

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_router.py -v`
Expected: 新用例 FAIL(fast path 仍直接 execute)。

- [ ] **Step 4: 实现**

`maa_remote/router.py`:
```python
from maa_remote.preview import plan_preview

FAST_PATH = {"跑日常", "日常", "daily", "跑一下日常", "托管", "托管一下"}
CONFIRM_WORDS = {"确认", "确定", "是", "yes", "y", "1", "开始"}
CANCEL_WORDS = {"取消", "算了", "不", "no", "n"}
SKIP_CONFIRM_PREFIX = "直接"
```

`route` / 新增 `_route_fresh` / `_handle_confirm` / `_maybe_confirm` 改为:
```python
    def route(self, msg: Msg) -> RouteResult:
        pending_confirm = self._pending_confirm.get(msg.chat_id)
        if pending_confirm and self.now_fn() < pending_confirm[1]:
            handled = self._handle_confirm(msg, pending_confirm[0])
            if handled is not None:
                return handled
            # 非确认/取消词:作废旧计划,当新指令处理
            self._pending_confirm.pop(msg.chat_id, None)
        elif pending_confirm:
            self._pending_confirm.pop(msg.chat_id, None)

        pending_selection = self._pending_selection.get(msg.chat_id)
        if pending_selection and self.now_fn() < pending_selection[1]:
            return self._handle_selection(msg, pending_selection[0])
        if pending_selection:
            self._pending_selection.pop(msg.chat_id, None)

        return self._route_fresh(msg)

    def _route_fresh(self, msg: Msg) -> RouteResult:
        text = msg.text.strip()
        if text in FAST_PATH:
            plan = TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks)
            return self._maybe_confirm(msg, plan)
        if text.startswith(SKIP_CONFIRM_PREFIX) and text[len(SKIP_CONFIRM_PREFIX):].strip() in FAST_PATH:
            plan = TaskPlan.daily(self.cfg.maa.fight, self.cfg.maa.daily_tasks)
            return self._maybe_confirm(msg, plan, skip_confirm=True)

        plan_data = self._llm_plan(msg.text)
        if plan_data is None:
            return RouteResult(kind="reply", reply="没太懂，你是想跑日常还是刷某个具体关卡？")

        action = plan_data.get("action")
        if action == "ask_stage_selection":
            return self._start_selection(msg)
        if action == "clarify":
            return RouteResult(
                kind="reply",
                reply=plan_data.get("clarify_question") or "能说得再具体点吗？",
            )
        if action == "reject":
            return RouteResult(kind="reply", reply="这个我帮不上，我只负责跑明日方舟日常/刷关卡。")

        return self._maybe_confirm(msg, TaskPlan.from_llm_dict(plan_data, self.cfg.maa.fight))

    def _maybe_confirm(self, msg: Msg, plan: TaskPlan, skip_confirm: bool = False) -> RouteResult:
        fight = plan.fight
        spend = fight.enable and (fight.stone > 0 or fight.medicine > 0)
        need_confirm = spend or (self.cfg.confirm.mode == "always" and not skip_confirm)
        if not need_confirm:
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)

        text = plan_preview(plan, self.cfg)
        if spend:
            text = "⚠️ 本计划包含花费（碎石/动用囤药），请核对后再确认！\n" + text
        self._pending_confirm[msg.chat_id] = (plan, self.now_fn() + self.cfg.confirm.ttl_s)
        return RouteResult(kind="reply", reply=text)

    def _handle_confirm(self, msg: Msg, plan: TaskPlan) -> RouteResult | None:
        text = msg.text.strip().lower()
        if text in CONFIRM_WORDS:
            self._pending_confirm.pop(msg.chat_id, None)
            return RouteResult(kind="execute", reply=self.cfg.runtime.ack_reply, plan=plan)
        if text in CANCEL_WORDS:
            self._pending_confirm.pop(msg.chat_id, None)
            return RouteResult(kind="reply", reply="好的，已取消。")
        return None
```
(原 `route` 里 FAST_PATH/LLM 段整体挪进 `_route_fresh`,别留重复代码。`_handle_selection`、`_start_selection`、`_llm_plan` 不动。)

- [ ] **Step 5: 跑测试通过 + 全量回归 + 意图回归**

Run: `.venv/Scripts/python -m pytest tests/test_router.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS。(evals 意图回归依赖 DeepSeek key,本任务不动 prompts/schema,跳过。)

- [ ] **Step 6: Commit**

```bash
git add maa_remote/router.py tests/test_router.py
git commit -m "feat: universal plan confirmation with preview, skip prefix and re-route on new command"
```

---

### Task 8: __main__ 接线(锚点消息 + ProgressSender)+ 本机配置更新

**Files:**
- Modify: `maa_remote/__main__.py`
- Modify: `config.example.toml`(ack_reply 文案)
- Modify: 本机 `config.toml`(未跟踪,新增节 + ack_reply)
- Test: `tests/test_main.py`(追加)

**Interfaces:**
- Consumes: Task 4 `ProgressSender`、Task 3 `send_reply`(返回 message_id)、Task 6 `execute(on_event=...)`。
- Produces: `handle_message` 执行分支——ack 回复即锚点消息,其 message_id 交给 `ProgressSender`;`cfg.progress.enable=False` 时不建 sender、`on_event=None`;执行结束(成功或失败)后 `sender.flush()` 再 `report`。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_main.py`)

(`ExecResult`/`Fight` 在该文件顶部的现有 import 里已经有了,不用重复加。)

```python
def test_execute_path_passes_progress_callback(tmp_path):
    cfg = _cfg(tmp_path)
    seen = {}
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))

    def fake_exec(plan, cfg2, task_dir, **kw):
        seen["on_event"] = kw.get("on_event")
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={}, error=None)

    handle_message(
        _msg(), router, cfg, threading.Lock(), OKLLM(), "bot", str(tmp_path / "tasks"),
        runner=_runner_recording([]), execute_fn=fake_exec, thread_factory=ImmediateThread,
    )
    assert callable(seen["on_event"])


def test_progress_disabled_passes_none_callback(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.progress.enable = False
    seen = {}
    plan = TaskPlan(action="run", fight=Fight(enable=True))
    router = FakeRouter(RouteResult(kind="execute", reply=cfg.runtime.ack_reply, plan=plan))

    def fake_exec(plan, cfg2, task_dir, **kw):
        seen["on_event"] = kw.get("on_event")
        return ExecResult(ok=True, exit_code=0, raw_log="", facts={}, error=None)

    handle_message(
        _msg(), router, cfg, threading.Lock(), OKLLM(), "bot", str(tmp_path / "tasks"),
        runner=_runner_recording([]), execute_fn=fake_exec, thread_factory=ImmediateThread,
    )
    assert seen["on_event"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_main.py -v`
Expected: 新用例 FAIL(`on_event` 未传)。

- [ ] **Step 3: 实现**

`maa_remote/__main__.py` 顶部加 `from maa_remote.progress import ProgressSender`。`handle_message` 中锁获取之后改为:
```python
    anchor_id = None
    if route_result.reply:
        anchor_id = send_reply(msg.message_id, route_result.reply, identity, runner=runner)

    sender = None
    if cfg.progress.enable:
        sender = ProgressSender(anchor_id, msg.message_id, identity, cfg.progress.style, runner=runner)

    def _job() -> None:
        try:
            result = execute_fn(
                route_result.plan, cfg, task_dir, runner=runner,
                on_event=sender.handle if sender is not None else None,
            )
            if sender is not None:
                sender.flush()
            report(result, msg, llm, identity, runner=runner)
        except Exception as exc:
            log.exception("worker 执行未捕获异常")
            send_reply(msg.message_id, f"执行崩了：{exc}", identity, runner=runner)
        finally:
            lock.release()

    thread_factory(target=_job, daemon=True).start()
```

- [ ] **Step 4: 配置文案与本机 config.toml**

1. `config.example.toml` 的 `[runtime] ack_reply` 改为 `"🚀 已开始，过程进度看本条消息的话题"`(加注释:`# style=flat 时建议改成"🚀 已开始，进度会陆续发出"`)。
2. 本机 `config.toml`(未被 git 跟踪):同步 ack_reply 文案;追加 `[progress]`/`[confirm]` 节(值同 example 默认);若 Task 0 notes 结论是 asst.log tail,在 `[maa]` 填 `asst_log_path`;若结论需要 `MAA_LOG`,在 `start.bat` 里补 `set MAA_LOG=...`(查看 notes)。

- [ ] **Step 5: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_main.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS(旧 test_main 用例的 fake 都用 `**kw`,天然兼容)。

- [ ] **Step 6: Commit**

```bash
git add maa_remote/__main__.py config.example.toml tests/test_main.py
git commit -m "feat: wire anchor message and progress sender into main loop"
```

---

### Task 9: 端到端冒烟(人工,与用户一起)

**Files:** 无代码改动;结果记录到 `tests/fixtures/maa_progress_notes.md` 追加一节「冒烟结论」。

前置:重启服务(`start.bat`)。逐项走,任何一步不符合预期就停下修:

- [ ] 发「跑日常」→ 收到 📋 计划预告(六个模块 + 10 分钟提示)
- [ ] 回「1」→ 收到 🚀 锚点消息;模拟器开始拉起
- [ ] 锚点消息的**话题**里陆续出现:拉起模拟器 → 模拟器就绪 → 启动游戏 → 公招 → 基建 → …(合并样式「✅ X完成 → Y中…」)
- [ ] 跑完后主聊天收到润色总结;主聊天全程只有预告、锚点、总结三条(预告算确认交互,可接受)
- [ ] **手机端**检查话题消息的通知体验:锁屏/横幅是否打扰、是否能看到进度。不满意 → `config.toml` 改 `style = "flat"` 重启再验一轮
- [ ] 发「直接跑日常」→ 跳过预告直接开跑
- [ ] 发「跑日常」→ 回「取消」→ 收到「已取消」;再回「1」→ 收到"没太懂"类回复(pending 已清)
- [ ] 发「跑日常」→ 不确认,直接发「刷1-7两次」→ 收到**新**计划预告(旧计划作废)
- [ ] 服务日志(`logs/maa_remote.log`)无异常堆栈
- [ ] 结论写入 notes 并 commit:`git add tests/fixtures/maa_progress_notes.md && git commit -m "docs: e2e smoke results for progress and confirm"`

---

## 附录 A: 换专用自建应用(运维,用户 + Claude 一起做,非 Codex 任务)

1. 飞书开放平台创建自建应用(命名如「明日方舟日常助手」),开启**机器人**能力。
2. 权限:IM 接收消息、发送消息;事件订阅 `im.message.receive_v1`,订阅方式选**长连接**;创建版本并发布,可用范围包含本人。
3. 本机执行 `lark-cli config init`(填新 app_id/app_secret)→ `lark-cli auth login`。
4. 检查 `config.toml` 的 `[lark] allowed_sender_open_id`:留空(运行时自动解析)或更新为新应用下的 open_id——**open_id 按应用隔离,换应用必变,严禁沿用旧值**。
5. 给新 bot 发一条消息激活会话 → 重启服务 → 发「跑日常」验证全链路。
6. 仅提供 bot token 不可行:长连接与令牌刷新需要 app_id+secret,这是 lark-cli 的绑定机制。

---

## Review 流程(Claude)

每个 Task 完成后 review 一次 commit:重点核对 ①测试是否真的先失败后通过(看测试是否可能假绿);②Global Constraints 红线(尤其"进度失败不影响执行"“花费无条件确认”);③与 spec `docs/superpowers/specs/2026-07-05-progress-and-confirm-design.md` 的一致性。Task 2 额外核对解析规则与 fixture 的对应关系;Task 9 由用户亲自验收。
