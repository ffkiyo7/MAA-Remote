# MAA_remote —— 详细设计 Spec（第 2 段）

> 配套文档：`CONTEXT.md`（目标 / 已定决策 / 架构方案 A）；实现计划：`docs/plans/2026-07-04-maa-remote.md`。本文件只展开**模块接口 + 配置 + 提示词契约 + 错误处理**。
> 状态：设计定稿（2026-07-04 评审修订版），待落地。

---

## 0. 本机实测环境快照（落地基准）

| 组件 | 实测值 / 路径 |
|---|---|
| Python | 3.14.3 |
| Node | v24.14.0 |
| lark-cli | **1.0.63**（npm 包 `@larksuite/cli`，全局）；身份 = **bot**，appId `cli_a9429c3b63f89cc1`，登录用户 open_id `ou_11bdc5069eb44da6ca4951cb8cd03ff2`（user token 已过期，仅 bot 可用） |
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
├─ README.md               # 安装/运行/自启/日志（运维入口）
├─ config.toml            # 运行配置（见 §2；含路径/身份/默认值，key 走 env；gitignore）
├─ config.example.toml    # 提交到库的模板（无敏感值）
├─ start.bat              # 双击启动 / 挂任务计划程序用
├─ docs/plans/            # 实现计划
├─ prompts/
│  └─ router.system.md    # 意图识别系统提示（能力目录+准则+few-shot）§5
├─ schemas/
│  └─ task_plan.schema.json  # TaskPlan JSON Schema §3
├─ evals/
│  └─ router_cases.jsonl  # 意图识别回归用例 §5
├─ logs/                  # 运行日志（gitignore）
└─ maa_remote/            # 源码（Python）
   ├─ __main__.py         # 主循环 + 单飞锁 + 日志 §4.6
   ├─ config.py           # 读 config.toml + env + lark 身份（解析失败 fail-fast）
   ├─ procutil.py         # subprocess 包装：强制 UTF-8（Windows 默认 GBK 会坏中文）
   ├─ models.py           # Msg / TaskPlan / ExecResult / StageInfo / RouteResult
   ├─ listener.py         # §4.1（含新鲜度过滤 + 断线退避重启）
   ├─ router.py           # §4.2（含待选关卡 + 碎石/囤药确认两个状态机）
   ├─ stage_catalog.py    # §4.3
   ├─ executor.py         # §4.4
   ├─ reporter.py         # §4.5
   ├─ llm.py              # DeepSeek 客户端（JSON模式+缓存+重试）
   └─ eval_router.py      # evals 回归跑分器（python -m maa_remote.eval_router）
```

---

## 2. `config.toml` 完整结构

```toml
[lark]
allowed_sender_open_id = ""      # 空=运行时从 auth status 自动锁定当机登录者；解析不到→启动报错（防静默吞消息）
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
core_dir       = "D:/Desktop/MAA-v5.20.0-beta.3-win-x64"   # 复用 GUI 的 MaaCore.dll；执行时注入 MAA_CORE_DIR
resource_dir   = "D:/Desktop/MAA-v5.20.0-beta.3-win-x64/resource"
config_dir     = "%APPDATA%/loong/maa/config"   # maa-cli 配置目录；任务文件写 <config_dir>/tasks（显式配置，不再从 cache 路径推导）
stage_activity_json = "%LOCALAPPDATA%/loong/maa/cache/StageActivityV2.json"
client         = "Official"    # StageActivity 区服键
hot_update_before_catalog = true   # 列关卡前先 maa hot-update 刷新
task_timeout_s = 3600          # 单次 maa run 上限（秒）

# 快速路径"跑日常"的固定子任务序列（fight 也由此列表控制，语义统一）
daily_tasks = ["startup", "recruit", "infrast", "mall", "award", "fight"]

[maa.fight]                     # "揉揉乐"默认（见 CONTEXT §二）
stage             = ""          # 空=当前/上次关卡；活动关卡由 StageCatalog 交互决定
expiring_medicine = true
medicine          = 0
stone             = 0           # 绝不碎石

[emulator]                       # 本机实测值，完整见项目根 config.toml
kind          = "mumu"
vmindex       = 0
# ⚠️ 可执行文件路径含空格必须加双引号：这两条命令会被 shlex.split 切分
launch_cmd    = '"D:/Program Files/YXArkNights-12.0/shell/MuMuManager.exe" control -v 0 launch'
shutdown_cmd  = '"D:/Program Files/YXArkNights-12.0/shell/MuMuManager.exe" control -v 0 shutdown'
adb_path      = 'D:/Program Files/YXArkNights-12.0/shell/adb.exe'
adb_serial    = "127.0.0.1:16384"   # port_forward.adb = 16384
boot_timeout_s = 120
close_after   = false

[runtime]
busy_reply       = "正在跑中，稍等 🐢"
ack_reply        = "🟡 收到，开始跑…"
selection_ttl_s  = 300          # 待选关卡 / 待确认状态过期时间
max_msg_age_s    = 300          # 忽略距今超过此秒数的旧消息（防服务重启后积压事件轰炸）
log_file         = "logs/maa_remote.log"
```

> 路径里的 `%VAR%` 由 `config.py` 展开环境变量后再用。
> 全项目所有 `subprocess` 调用统一走 `procutil.run_utf8`（强制 `encoding="utf-8", errors="replace"`）——Windows 默认 cp936 会把 maa/lark 的中文输出弄成乱码或直接抛 `UnicodeDecodeError`。

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
        "medicine": {"type":"integer","minimum":0,"maximum":999},  // >0 需二次确认
        "stone":    {"type":"integer","minimum":0,"maximum":999}   // >0 需二次确认
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
listen(cfg, allowed_sender) -> Iterator[Msg]   # 内部无限循环，子进程挂了自动重启
```
- 子进程 `lark-cli event consume im.message.receive_v1 --as bot`，逐行读 NDJSON（`encoding="utf-8"`）。
- 过滤三道：① `sender_open_id == allowed_sender`（allowed_sender 由启动时解析，解析不到直接启动失败，**绝不**以空串运行——否则所有消息被静默过滤）；② 仅文本消息；③ **新鲜度**：`create_time` 距今超过 `max_msg_age_s` 的旧消息直接丢弃并记日志（防服务重启后积压事件当新指令执行）。
- 断线：子进程退出/异常 → 记日志 → 指数退避（1s 起，×2，封顶 60s）重启；收到有效消息后退避归位。**实现必须真的写这个循环**，不是注释。

### 4.2 Router（**有状态**：待选关卡 + 碎石/囤药确认两个状态机）
```
route(msg: Msg) -> RouteResult
```
状态（均按 `chat_id` 存，TTL=`selection_ttl_s`）：
- `pending_selection[chat_id] = (stages: list[StageInfo], expire_at)`
- `pending_confirm[chat_id] = (plan: TaskPlan, expire_at)`

流程（顺序即优先级）：
1. **确认状态机**：有未过期待确认 plan → `msg.text` 为「确认/确定/yes/y」→ 产出该 plan 执行；「取消/算了/no/n」→ 丢弃；其他 → 回"回复「确认」执行，或「取消」放弃"。
2. **待选状态机**：有未过期待选状态 → 把 `msg.text` 当**选择**解析（编号 / 关卡号 / "取消"）→ 命中则产出 fight TaskPlan（**`startup=true`**——模拟器多半是冷启动，游戏未开，没 StartUp 后续全挂）；否则回"没听懂，回编号或关卡号"。
3. **快速路径**：`text` 精确命中 `{跑日常, 日常, daily, 跑一下日常, 托管, 托管一下}` → 用 `daily_tasks` 直接组 TaskPlan。
4. **LLM 路径**：其余 → DeepSeek（JSON 模式）出 TaskPlan，schema 校验，失败重试 `max_retries` 次，仍失败 → `action=clarify`。
5. 若 TaskPlan `action=ask_stage_selection` → 调 StageCatalog，写入 `pending_selection`，回菜单，**不进执行**。
6. **安全阀**：任何将执行的 plan 若 `fight.stone > 0` 或 `fight.medicine > 0` → 不直接执行，写入 `pending_confirm`，回"⚠️ 这个计划会碎 X 颗源石/动用 Y 瓶囤药。回复「确认」执行，「取消」放弃"。
7. 其余 `action=run` → 回 `ack_reply` → 交 Executor。

### 4.3 StageCatalog
```
fetch_open_stages() -> list[StageInfo]
format_menu(stages) -> str          # 交给 LLM 润色成带编号菜单
resolve_selection(text, stages) -> str | None   # 编号/关卡号 → stage code
```
- 数据源：读 `stage_activity_json`；`hot_update_before_catalog=true` 时先跑 `maa hot-update`。
- 过滤：按 `client` 取 `sideStoryStage`，用 `UtcStartTime <= now <= UtcExpireTime` 只留**当前开放**的活动关卡。
- 空列表（无活动）→ Router 回"当前没有开放的活动关卡，要不要刷常规关/当前关？"

### 4.4 Executor（**单飞**，全局锁；执行本体跑在 worker 线程里，见 §4.6）
```
execute(plan: TaskPlan, cfg, task_dir) -> ExecResult
```
1. `ensure_emulator()`：`shlex.split(launch_cmd)` 跑启动命令（**config 里 exe 路径带引号**，shlex 才能正确切分含空格路径）→ 轮询循环内**每轮**先 `adb connect <serial>` 再 `adb -s <serial> get-state`（MuMu 冷启动时端口后开，connect 必须重试，不能只连一次）→ `device` 即就绪；超时 `boot_timeout_s` → `EmulatorError`。
2. `build_task_file(plan, client)`：TaskPlan → maa-cli 自定义任务 JSON（顶层 `{"tasks":[...]}`），写到 `<config_dir>/tasks/<name>.json`。
3. 跑 `maa run <name> -a <adb_serial> --batch`（**保留 summary**——它是最好解析的结构化输出，不加 `--no-summary`），env 注入：
   - `MAA_CONFIG_DIR = config_dir`
   - `MAA_CORE_DIR = core_dir`（**必须注入**，maa-cli 自身没装 core，复用 GUI 的）
   - `MAA_RESOURCE_DIR = resource_dir`（maa-cli 若不识别该变量则无害；dry-run 时校验 resource 已加载）
   超时 `task_timeout_s`；subprocess 统一 UTF-8。
4. `close_after` → 关模拟器。
5. 组 `ExecResult`（`parse_maa_log` 抽 facts：优先截取 summary 段落，正则兜底，`raw_tail` 保底）。

### 4.5 Reporter
```
report(result: ExecResult, chat_id: str) -> None
```
- `parse_maa_log`：从日志抽 理智消耗/掉落 · 公招次数 · 信用 · 基建换班/收菜 · 异常。
- `deepseek_polish(facts)`：LLM 润色成自然语言总结；**LLM 超时/失败 → 回退裸事实模板，不静默**。
- `lark-cli im +messages-reply --message-id <触发消息> ...` 回到原会话。

### 4.6 主循环 + 单飞锁（执行在 worker 线程，监听不阻塞）

> ⚠️ 设计要点：如果执行和监听在同一线程串行，`busy_reply` 是死代码——执行 maa 的
> 30 分钟里锁早被同线程释放前根本不会有第二次 acquire，新消息只是堆在管道里，
> 跑完后被**依次当作新指令执行**。所以执行必须放 worker 线程，主线程持续读消息。

```python
lock = threading.Lock()          # 主线程 acquire，worker 线程 release（threading.Lock 允许跨线程 release）
for msg in listen(cfg, allowed):             # listen 内部自带断线重启
    rr = router.route(msg)
    if rr.kind == "reply":
        send_reply(msg, rr.reply); continue
    if not lock.acquire(blocking=False):     # 有任务在跑 → 立刻回 busy
        send_reply(msg, busy_reply); continue
    send_reply(msg, ack_reply)
    def job():                                # worker：执行 + 汇报 + 兜底
        try:
            report(execute(rr.plan, cfg, task_dir), msg)
        except Exception as e:
            send_reply(msg, f"执行崩了：{e}")   # 任何异常都有回音
        finally:
            lock.release()
    threading.Thread(target=job, daemon=True).start()
```
**绝不并发**：执行中来消息 → 立刻 `busy_reply`（真的会发生，因为主线程没被执行阻塞）。
日志：`logging` + `RotatingFileHandler` 写 `runtime.log_file`（UTF-8），同时输出控制台。

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
| 启动 | allowed_sender 解析不到（auth status 无 userOpenId） | **启动直接报错退出**，提示在 config 显式填 open_id——绝不带空过滤器运行（会静默吞掉所有消息） |
| Listener | 事件子进程退出/异常 | 指数退避重启（1s→60s 封顶）；记日志 |
| Listener | 消息过旧（> `max_msg_age_s`） | 丢弃并记日志（防积压事件当新指令） |
| Router-LLM | DeepSeek 超时 / 非法 JSON | 重试 `max_retries`；仍失败→`clarify`，回"没太懂，能换种说法吗" |
| Router-安全阀 | plan 含碎石/动囤药（stone>0 或 medicine>0） | 不执行，先回确认问句；「确认」才放行，TTL 过期作废 |
| StageCatalog | hot-update 失败 / 文件缺失 | 用旧缓存；都没有→回"暂时取不到活动关卡" |
| 模拟器 | `launch_cmd` 失败 / adb 超时 | 回"模拟器没起来/连不上，检查 MuMu"（connect 在轮询内重试） |
| maa 执行 | 非零退出 / 崩溃 / 超 `task_timeout_s` | 回"MAA 跑挂了（退出码 X）"+日志尾部关键行 |
| worker 线程 | 任何未捕获异常 | 回"执行崩了：…"+记日志，**必释放锁** |
| Reporter-LLM | 润色超时/失败 | 回退裸事实模板 |
| 并发 | 执行中来新消息 | 立刻 `busy_reply`（主线程未被执行阻塞，见 §4.6） |

---

## 7. 落地待办（需向你采集 / 待实测）

1. ~~**MuMu 12** 路径/端口/adb~~ ✅ 已采集并写入 `config.toml`（`YXArkNights-12.0`，实例0，端口 16384）。落地只剩**实跑验证** `ensure_emulator()`：launch → 轮询（connect+get-state）到 device ready。
   > 📌 连接方式已定死用**模拟器(ADB)**。核实结论：**maa-cli 只支持 ADB（模拟器/安卓设备）+ PlayCover(macOS) + Waydroid(Linux)，不支持 Win32/PC 客户端桌面控制**。PC 客户端那套 Win32 控制是 MAA GUI 独有 beta，maa-cli 未暴露；且用 PC 客户端就得回退 GUI，与"不碰 GUI"决策冲突。故本机 PC 客户端弃用，走 MuMu 12。来源：docs.maa.plus/en-us/manual/cli/config.html
2. **maa-cli 复用 GUI core**：落地时验证注入 `MAA_CORE_DIR` 指向 GUI 目录后 `maa version` / dry-run 正常（GUI 已更到 v6.13，core 版本匹配最新 resource）。
3. **DeepSeek API key**：设进环境变量 `DEEPSEEK_API_KEY`。
4. **飞书事件订阅**：确认应用后台已开 `im.message.receive_v1` 长连接订阅、bot 有收发消息权限（冒烟测一条）。
5. **`lark-cli auth status` 输出格式核对**：是否 JSON、bot-only 态（user token 过期）下有无 `userOpenId`——这决定 allowed_sender 自动解析是否可用；不可用则 config 显式填。
6. ~~**maa 日常任务编排**~~ ✅ 已定：自定义 task 文件（`maa run <name>`），见 §4.4。
7. ~~实现计划~~ ✅ → `docs/plans/2026-07-04-maa-remote.md`（评审修订版，12 个 TDD 任务 + 端到端冒烟清单）。

