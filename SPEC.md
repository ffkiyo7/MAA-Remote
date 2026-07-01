# MAA_remote —— 详细设计 Spec（第 2 段）

> 配套文档：`CONTEXT.md`（目标 / 已定决策 / 架构方案 A）。本文件只展开**模块接口 + 配置 + 提示词契约 + 错误处理**。
> 状态：设计定稿，待落地。最后更新：2026-07-02。

---

## 0. 本机实测环境快照（落地基准）

| 组件 | 实测值 / 路径 |
|---|---|
| Python | 3.14.3 |
| Node | v24.14.0 |
| lark-cli | **1.0.63**（npm 包 `@larksuite/cli`，全局）；身份 = **bot**，appId `cli_a9429c3b63f89cc1`，登录用户 黄文轩 `ou_11bdc5069eb44da6ca4951cb8cd03ff2`（user token 已过期，仅 bot 可用） |
| 收消息事件 | `lark-cli event consume im.message.receive_v1`（bot） |
| 发消息 | `lark-cli im +messages-send` / `+messages-reply`（user/bot 均可） |
| maa-cli | **v0.7.5**，`%LOCALAPPDATA%\Programs\maa-cli\maa.exe`（预编译，非 cargo） |
| maa-cli 数据/缓存 | data=`%APPDATA%\loong\maa\data` · cache=`%LOCALAPPDATA%\loong\maa\cache` · config=`%APPDATA%\loong\maa\config` |
| 活动关卡数据源 | `%LOCALAPPDATA%\loong\maa\cache\StageActivityV2.json`（`maa hot-update` 刷新） |
| MaaCore | maa-cli **自身未装 core**；复用 MAA GUI 的 `MaaCore.dll`+`resource`（见下） |
| MAA GUI | `D:\Desktop\MAA-v5.20.0-beta.3-win-x64`（**已更新到 v6.13.0，文件夹名仍是旧名，属误称**） |
| 模拟器 | **MuMu 12（明日方舟定制版）** `D:\Program Files\YXArkNights-12.0\`。实例 index=0「明日方舟-MuMu模拟器12」。启停：`shell\MuMuManager.exe control -v 0 launch/shutdown` |
| adb | `D:\Program Files\YXArkNights-12.0\shell\adb.exe`；**端口 16384**（`vm_config.json` 的 `port_forward.adb`）→ serial `127.0.0.1:16384` |

> ⚠️ 硬编码禁区（延续 CONTEXT 第四节）：上表所有值进 `config.toml`，代码运行时读，不写死。身份运行时读 `lark-cli auth status`。

---

## 1. 仓库结构（提案）

```
MAA-remote/
├─ CONTEXT.md              # 背景/决策
├─ SPEC.md                 # 本文件
├─ config.toml            # 运行配置（见 §2；含路径/身份/默认值，key 走 env）
├─ config.example.toml    # 提交到库的模板（无敏感值）
├─ prompts/
│  └─ router.system.md    # 意图识别系统提示（能力目录+准则+few-shot）§5
├─ schemas/
│  └─ task_plan.schema.json  # TaskPlan JSON Schema §3
├─ evals/
│  └─ router_cases.jsonl  # 意图识别回归用例 §5
└─ maa_remote/            # 源码（Python）
   ├─ __main__.py         # 主循环 + 单飞锁 §4.6
   ├─ config.py           # 读 config.toml + env + lark 身份
   ├─ listener.py         # §4.1
   ├─ router.py           # §4.2（含待选状态机）
   ├─ stage_catalog.py    # §4.3
   ├─ executor.py         # §4.4
   ├─ reporter.py         # §4.5
   └─ llm.py              # DeepSeek 客户端（JSON模式+缓存+重试）
```

---

## 2. `config.toml` 完整结构

```toml
[lark]
allowed_sender_open_id = ""      # 空=运行时从 auth status 自动锁定当机登录者；换私人组织账号零改代码
app_id                 = ""      # 空=用 lark-cli 当前应用；多应用时可显式指定
identity               = "auto"  # 传给 lark-cli --as
event_key              = "im.message.receive_v1"

[llm]
provider           = "deepseek"
model              = "deepseek-chat"
base_url           = "https://api.deepseek.com"
api_key_env        = "DEEPSEEK_API_KEY"   # 只存 env 名，key 不进文件
request_timeout_s  = 30
max_retries        = 1                     # JSON schema 校验失败时重试次数
cache_system_prompt = true                  # 固定系统提示走缓存，省钱关键

[maa]
maa_cli_path   = "%LOCALAPPDATA%/Programs/maa-cli/maa.exe"
core_dir       = "D:/Desktop/MAA-v5.20.0-beta.3-win-x64"   # 复用 GUI 的 MaaCore.dll
resource_dir   = "D:/Desktop/MAA-v5.20.0-beta.3-win-x64/resource"
stage_activity_json = "%LOCALAPPDATA%/loong/maa/cache/StageActivityV2.json"
client         = "Official"    # StageActivity 区服键
hot_update_before_catalog = true   # 列关卡前先 maa hot-update 刷新

# 快速路径"跑日常"的固定子任务序列
daily_tasks = ["startup", "recruit", "infrast", "mall", "award"]

[maa.fight]                     # "揉揉乐"默认（见 CONTEXT §二）
stage             = ""          # 空=当前/上次关卡；活动关卡由 StageCatalog 交互决定
expiring_medicine = true
medicine          = 0
stone             = 0           # 绝不碎石

[emulator]                       # 本机实测值，完整见项目根 config.toml
kind          = "mumu"
vmindex       = 0
launch_cmd    = 'D:/Program Files/YXArkNights-12.0/shell/MuMuManager.exe control -v 0 launch'
shutdown_cmd  = 'D:/Program Files/YXArkNights-12.0/shell/MuMuManager.exe control -v 0 shutdown'
adb_path      = 'D:/Program Files/YXArkNights-12.0/shell/adb.exe'
adb_serial    = "127.0.0.1:16384"   # port_forward.adb = 16384
boot_timeout_s = 120
close_after   = false

[runtime]
busy_reply       = "正在跑中，稍等 🐢"
ack_reply        = "🟡 收到，开始跑日常…"
selection_ttl_s  = 300          # 待选关卡状态过期时间
```

> 路径里的 `%VAR%` 由 `config.py` 展开环境变量后再用。

---

## 3. 数据模型

### Msg（Listener → Router）
```python
@dataclass
class Msg:
    text: str
    chat_id: str
    message_id: str
    sender_open_id: str
    create_time: int
```

### TaskPlan（Router 产出，schema 强校验）
`schemas/task_plan.schema.json`（要点）：
```jsonc
{
  "type": "object",
  "required": ["action"],
  "properties": {
    "action": { "enum": ["run", "ask_stage_selection", "clarify", "reject"] },
    "startup": { "type": "boolean", "default": true },
    "recruit": { "type": "object", "properties": {
        "enable": {"type":"boolean"}, "max_times": {"type":"integer","minimum":0,"maximum":4} } },
    "infrast": { "type": "object", "properties": { "enable": {"type":"boolean"} } },
    "mall":    { "type": "object", "properties": { "enable": {"type":"boolean"} } },
    "award":   { "type": "object", "properties": { "enable": {"type":"boolean"} } },
    "fight":   { "type": "object", "properties": {
        "enable": {"type":"boolean"},
        "stage":  {"type":"string"},          // "" = 当前关；"CE-6"/"UR-8" 等
        "times":  {"type":"integer","minimum":1},
        "expiring_medicine": {"type":"boolean"},
        "medicine": {"type":"integer","minimum":0},
        "stone":    {"type":"integer","minimum":0}
    } },
    "clarify_question": { "type": "string" },  // action=clarify 时回给用户的问题
    "note": { "type": "string" }               // 自然语言原意，给 Reporter 参考
  }
}
```
- `action=run`：直接执行。
- `action=ask_stage_selection`：用户想刷活动但没定具体关 → 触发 StageCatalog 菜单。
- `action=clarify`：意图不清 → 回 `clarify_question`。
- `action=reject`：非任务请求（闲聊/超范围）→ 礼貌拒答。

### ExecResult（Executor → Reporter）
```python
@dataclass
class ExecResult:
    ok: bool
    exit_code: int
    raw_log: str
    facts: dict          # parse 后的结构化事实（理智/公招/信用/基建/异常）
    error: str | None    # 失败原因（模拟器/adb/maa/超时）
```

### StageInfo（StageCatalog → Router/LLM）
```python
@dataclass
class StageInfo:
    activity_name: str   # 如"泡影苍霆"
    code: str            # "TD-8"
    drop: str            # "推荐刷最快的"
    expire_utc: str      # 结束时间（用于提示紧迫度）
```

---

## 4. 模块接口

### 4.1 Listener
```
listen() -> Iterator[Msg]
```
- 子进程 `lark-cli event consume im.message.receive_v1 --as auto`，逐行读 NDJSON。
- 过滤：仅 `sender_open_id == allowed_sender`（空则 = auth status 的 userOpenId）且为文本消息。
- 断线：子进程退出则重启（指数退避），并记日志。

### 4.2 Router（**有状态**：待选关卡状态机）
```
route(msg: Msg) -> RouteResult
```
状态：`pending_selection[chat_id] = { stages: list[StageInfo], expire_at }`（TTL=`selection_ttl_s`）。

流程：
1. 若该 `chat_id` 有未过期的待选状态 → 把 `msg.text` 当作**选择**解析（编号 / 关卡号 / "取消"）→ 命中则产出 `action=run` 的 fight TaskPlan；否则回"没听懂，回编号或关卡号"。
2. 否则走意图识别：
   - **快速路径**：`text` 精确命中 `{跑日常, 日常, daily}` → 用 `daily_tasks` 直接组 TaskPlan。
   - **LLM 路径**：其余 → DeepSeek（JSON 模式）出 TaskPlan，schema 校验，失败重试 `max_retries` 次，仍失败 → `action=clarify`。
3. 若 TaskPlan `action=ask_stage_selection` → 调 StageCatalog，写入 `pending_selection`，回菜单，**不进执行**。
4. 其余 `action=run` → 立刻回 `ack_reply` → 交 Executor。

### 4.3 StageCatalog
```
fetch_open_stages() -> list[StageInfo]
format_menu(stages) -> str          # 交给 LLM 润色成带编号菜单
resolve_selection(text, stages) -> str | None   # 编号/关卡号 → stage code
```
- 数据源：读 `stage_activity_json`；`hot_update_before_catalog=true` 时先跑 `maa hot-update`。
- 过滤：按 `client` 取 `sideStoryStage`，用 `UtcStartTime <= now <= UtcExpireTime` 只留**当前开放**的活动关卡。
- 空列表（无活动）→ Router 回"当前没有开放的活动关卡，要不要刷常规关/当前关？"

### 4.4 Executor（**单飞**，全局锁）
```
execute(plan: TaskPlan) -> ExecResult
```
1. `ensure_emulator()`：跑 `launch_cmd` → 轮询 `adb -s <serial> get-state` 到 `device`，超时 `boot_timeout_s` → `EmulatorError`。
2. `build_maa_args(plan)`：TaskPlan → maa-cli 调用。子任务映射：
   - `startup`→`maa startup`；整体日常用 `maa run <task-file>` 或逐子任务串联（落地定：优先自定义 task 文件）。
   - `fight`→`maa fight <stage> --medicine ... `（`expiring_medicine`/`stone` 通过 task 参数）。
   - core/resource 指向 `[maa].core_dir`/`resource_dir`（环境变量 `MAA_CORE_DIR` 或 config）。
3. 子进程跑 maa，实时收 stdout/stderr + exit_code。
4. `close_after` → 关模拟器。
5. 组 `ExecResult`（`parse_maa_log` 抽 facts）。

### 4.5 Reporter
```
report(result: ExecResult, chat_id: str) -> None
```
- `parse_maa_log`：从日志抽 理智消耗/掉落 · 公招次数 · 信用 · 基建换班/收菜 · 异常。
- `deepseek_polish(facts)`：LLM 润色成自然语言总结；**LLM 超时/失败 → 回退裸事实模板，不静默**。
- `lark-cli im +messages-reply --message-id <触发消息> ...` 回到原会话。

### 4.6 主循环 + 单飞锁
```python
lock = threading.Lock()   # 或进程级文件锁
for msg in listen():
    rr = router.route(msg)
    if rr.needs_execution:
        if not lock.acquire(blocking=False):
            reply(msg, busy_reply); continue
        try: report(executor.execute(rr.plan), msg.chat_id)
        finally: lock.release()
```
**绝不并发**：执行中来消息 → `busy_reply`。

---

## 5. 提示词契约三件套（意图识别质量保障）

> 运行时大脑是 **DeepSeek**，不是 Claude Code；"skill/agents.md 约束"落到 DeepSeek 上 = 版本化提示词契约。

| 文件 | 内容 |
|---|---|
| `prompts/router.system.md` | ① 能力目录（子任务清单 + 各参数含义）② 行为准则（绝不碎石/不动囤药，除非明确要求；范围外→reject）③ 列关卡交互协议（何时输出 `ask_stage_selection`）④ few-shot 示例（覆盖：跑日常 / 指定关卡次数 / 关子任务 / 刷活动 / 闲聊拒答） |
| `schemas/task_plan.schema.json` | §3 的 schema。DeepSeek 用 JSON 模式输出，代码强校验，非法带错误重试一次 |
| `evals/router_cases.jsonl` | `{input, expected_plan}` 用例集；改提示词后跑回归，防质量回退 |

- 系统提示固定 → DeepSeek 缓存 → 成本可忽略，因此可写厚（详尽 few-shot）。
- 开发期可用 Claude Code skill 辅助**维护这三个文件 / 跑 evals**（dev-time），运行时不依赖 Claude。

---

## 6. 错误处理与超时矩阵（每步都有明确失败消息，绝不静默）

| 环节 | 失败条件 | 兜底动作 |
|---|---|---|
| Listener | 事件子进程退出 | 指数退避重启；连续失败记日志 |
| Router-LLM | DeepSeek 超时 / 非法 JSON | 重试 `max_retries`；仍失败→`clarify`，回"没太懂，能换种说法吗" |
| StageCatalog | hot-update 失败 / 文件缺失 | 用旧缓存；都没有→回"暂时取不到活动关卡" |
| 模拟器 | `launch_cmd` 失败 / adb 超时 | 回"模拟器没起来/连不上，检查 MuMu" |
| maa 执行 | 非零退出 / 崩溃 | 回"MAA 跑挂了（退出码 X）"+日志尾部关键行 |
| Reporter-LLM | 润色超时/失败 | 回退裸事实模板 |
| 并发 | 执行中来新消息 | `busy_reply` |

---

## 7. 落地待办（需向你采集 / 待实测）

1. ~~**MuMu 12** 路径/端口/adb~~ ✅ 已采集并写入 `config.toml`（`YXArkNights-12.0`，实例0，端口 16384）。落地只剩**实跑验证** `ensure_emulator()`：launch → 轮询 adb 到 device ready。
   > 📌 连接方式已定死用**模拟器(ADB)**。核实结论：**maa-cli 只支持 ADB（模拟器/安卓设备）+ PlayCover(macOS) + Waydroid(Linux)，不支持 Win32/PC 客户端桌面控制**。PC 客户端那套 Win32 控制是 MAA GUI 独有 beta，maa-cli 未暴露；且用 PC 客户端就得回退 GUI，与"不碰 GUI"决策冲突。故本机 PC 客户端弃用，走 MuMu 12。来源：docs.maa.plus/en-us/manual/cli/config.html
2. **maa-cli 复用 GUI core**：落地时验证 `MAA_CORE_DIR` 指向 GUI 目录能让 `maa fight` 正常跑（GUI 已更到 v6.13，core 版本匹配最新 resource）。
3. **DeepSeek API key**：设进环境变量 `DEEPSEEK_API_KEY`。
4. **飞书事件订阅**：确认应用后台已开 `im.message.receive_v1` 长连接订阅、bot 有收发消息权限（冒烟测一条）。
5. **maa 日常任务编排**：定用"自定义 task 文件"还是逐子任务串联（§4.4）。
6. 写完 §5 三件套后：接 `writing-plans` 出实现计划 → 编码。
```

