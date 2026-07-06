# 会话记录：自启失效排查 + Hermes 迁移方案讨论

> 日期：2026-07-06
> 会话机器：**aimall（D:\Code\MAA-Remote，开发机）**——注意：不是跑服务的家里那台（E: 盘那台）。
> 用途：回家后在家里机器上打开本项目，让 Claude 读本文件，说"接着 2026-07-06 的讨论往下"即可续聊。
> 性质：本文是对话的忠实整理稿（用户提问为原意转述，回答保留全部技术结论），不是逐字原文。

---

## 议题 1：开机自启失效排查

**用户问题**：昨天 codex 配了一个快捷方式放在 Windows 用户某文件夹里，说开机会自动启动 remote 服务；今天在外面给飞书机器人发消息，家里电脑没反应。

**实测发现（在 aimall 这台机器上查的）**：
- 启动文件夹（`shell:startup`）只有 Snipaste，无 MAA-Remote 快捷方式；任务计划无 `MAA-Remote`；注册表 Run 键无相关项。
- 本目录**没有 `.venv`、没有 `config.toml`、没有 `logs/`**——这份 checkout 从未运行过。
- 本机无 MuMu、无 maa-cli、无 `DEEPSEEK_API_KEY`（user/machine/session 三个作用域都没有）、无 E 盘。
- **结论：aimall 是开发机。codex 的快捷方式和真正跑服务的环境都在家里那台（`E:\code\MAA-remote`），从这里看不到。** 昨晚的 v4-flash smoke（见 `docs/plans/2026-07-05-llm-v4-flash-thinking-smoke.md`）是在家里那台做的，key 设在了用户环境变量。

**失效原因按可能性排序**：
1. **Startup 快捷方式只在用户登录桌面后触发**——开机停在锁屏（如 Windows Update 自动重启后没人登录）就永远不会执行。无人值守场景的头号死因。
2. **电脑睡眠**——睡着的机器收不到任何飞书事件，与启动方式无关，必须 `powercfg` 禁睡眠。
3. **快捷方式放错文件夹**——如果放到了开始菜单 `Programs` 根目录而非 `Programs\Startup`，只是菜单项不会自启。
4. **起来了但后来死了**——`start.bat` 崩溃后 `pause` 挂住，无重启。listener 对 lark-cli 子进程有退避重启（`listener.py:150`），但 Python 主进程崩了没人管。

**到家排查清单（5 分钟定位）**：

```powershell
# ① 快捷方式在不在、指向对不对（或 Win+R 输入 shell:startup）
Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"

# ② 对比上次开机时间和日志最后活动时间
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Get-Content E:\code\MAA-remote\logs\maa_remote.log -Tail 20
```

判读：开机后日志完全无新条目 → 快捷方式没触发（原因 1/3）；有启动条目但中断 → 进程死了（原因 4）；日志一直正常但没回消息 → 查睡眠（原因 2）。另外任务管理器"启动应用"确认没被禁用。

---

## 议题 2：启动链路怎么做稳

启动链路拆三个独立环节，谁也替代不了谁：

| 环节 | Startup 快捷方式现状 | 谁能修 |
|---|---|---|
| ① 开机拉起（不依赖登录） | ❌ 必须登录 | Windows 服务 / schtasks ONSTART |
| ② 机器保持唤醒 | ❌ 没管 | 只有 `powercfg`，任何守护方案无解 |
| ③ 崩溃自动重启 | ❌ pause 挂住 | 服务恢复策略 / supervisor |

**原生方案（推荐先做，可靠性=Windows 本身）**：
- NSSM（或 WinSW）把服务包成 Windows 服务：开机即起、无需登录、崩溃自动重启、日志重定向；
- 或 `schtasks /Create /SC ONSTART /RU <用户> /RP <密码>` + 失败重启策略（README 里现在的 ONLOGON 同样依赖登录，要改）；
- `powercfg /change standby-timeout-ac 0` 禁接电睡眠。

Hermes 只覆盖环节 ③ + 远程可观测/远程重启（这是它真正的增量价值）；环节 ① 取决于 Hermes 自己怎么被拉起；环节 ② 谁都救不了。

---

## 议题 3：lark-cli 多应用混淆 + key 注入方式

**多应用混淆 → lark-cli 原生 profile 机制（本机实测确认存在）**：
- `lark-cli profile add/list/use/remove` + 全局 flag `--profile <name>`（单次调用指定 profile，不切全局状态）。
- 方案：给 MAA-Remote 建专用自建应用 + 专用 profile，服务所有调用带 `--profile maa-remote`；全局默认 profile 留给云文档 agent 等其他用途。
- 代码改动：`config.toml` 的 `[lark]` 加 `profile` 字段，三个调用点统一追加 flag：`listener.py:113`、`reporter.py:71`、`__main__.py:97`。不违反"不写死身份"原则（profile 名进机器专属 config.toml，身份仍由 profile 登录态运行时决定）。

**key 注入 → 支持项目 `.env`**：
- `config.py` 已是"从 env mapping 按 `api_key_env` 取值"结构，只需 `__main__` 启动时读项目根 `.env`（gitignore）合入环境，十几行；`start.bat` 检查同步调整。
- 坦诚结论：`.env` 与 setx 用户环境变量都是明文、同用户进程都可读，**理论安全增益边际**；实际收益是作用域（不污染全局环境、每项目独立、随目录迁移）。真要提安全等级用 Windows 凭据管理器（Python keyring），属锦上添花。

---

## 议题 4：封装成 Skill / Hermes 统一入口方案（本次会话的主线结论）

### 演进路径

用户设想：家里机器装 **Hermes Agent** 作常驻统一入口（原生飞书接入、多 Model Provider、成本可控，还会承担 MAA 之外的其他任务），MAA-Remote 封装成 skill 供其调用，开始前确认/过程/结果经 Hermes 的自建应用发到飞书。

**结论：可行，且是合理演进**——项目"两半"结构里，监听半边（listener/router/llm/reporter 监听侧，约半个代码库）整个交给 Hermes；执行半边收缩成纯执行层 skill。原先否掉"方案 C"（agent 当大脑）的成本理由，因多 provider 路由到便宜模型而不再成立；常驻开销因 Hermes 一守护多任务被摊薄。lark-cli 多应用混淆也随之基本消解（MAA 自己的飞书身份不存在了，你 DM 哪个 bot 哪个系统响应；议题 3 的 profile 建议降级为小卫生习惯）。

### 五个必须沉到 skill 脚本层的保证（不能只靠 agent 遵循指令）

1. **单飞锁**：Hermes 无内置任务互斥，skill CLI 必须自带锁文件，第二次调用直接返回"正在跑中"。
2. **碎石/囤药安全阀做成机械强制**：CLI 两步走——`plan` 输出执行预览（是否碎石/吃药）+ 一次性 confirm token；`start --confirm-token X` 只认该 plan 签发的 token。agent 物理上跳不过预览；指令层再要求"预览转发用户、确认后才 start"；stone/medicine 脚本默认恒 0、TTL 过期作废。
3. **长任务异步化**：一把日常 30–60 分钟，不能一次阻塞工具调用扛。`start` 拉起后台任务立刻返回 job id，`status` 读进度。
4. **进度推送选型（待定）**：(a) Hermes 轮询 status 转述——入口统一但烧 token；(b) 脚本直发飞书（复用现有 progress/reporter 机器）——便宜可靠，代价是可能双 bot 身份。倾向 (b) 或利用 Hermes 完成通知简化，到时候实测定。
5. **启动链路问题原封转移到 Hermes 头上**：服务化 + 禁睡眠照做，主语换成 Hermes。

### Hermes 官方规格核查（2026-07-06 读官方仓库 website/docs 最新版）

Hermes Agent = Nous Research 2026-02 开源（MIT）自托管 agent runtime。**项目年轻迭代快，安装建议 `-Tag` 钉版本。**

| 需求 | Hermes 机制 | 结论 |
|---|---|---|
| Windows 家用机常驻 | Win10/11 原生 **Tier 1**（不需要 WSL/Docker），PowerShell 一行安装 | ✅ |
| 飞书接入 | 官方适配器，**WebSocket 长连接**，无公网 IP 可用 | ✅ |
| 只响应本人 | `FEISHU_ALLOWED_USERS` 白名单（open_id） | ✅ |
| MAA 封装 skill | SKILL.md 兼容 agentskills.io（与 Claude skill 同源），带 `scripts/`；自动成为 `/maa` 斜杠命令，**飞书里直接可用**；`skills.external_dirs` 可指向本仓库 `skills/` 目录随 git 管理；`platforms: [windows]` | ✅ |
| 30–60 分钟长任务 | `terminal` 支持 `background=true` + **`notify_on_complete=true`（结束自动通知，无需轮询）**；`process` 工具 poll/log/wait/kill | ✅ 关键一环官方直接解了 |
| 开始前确认 | skill CLI token 门（核心）+ 内置 `clarify` 工具（消息平台发选择题） | ✅ |
| 过程/结果回飞书 | 回复自动走 gateway 回触发会话；home chat（`/set-home`）收 cron/跨平台通知 | ✅ |
| 多 provider/成本 | 任意 OpenAI 兼容端点 custom `base_url`（**DeepSeek key 直连可用**）；auxiliary 任务可配更便宜模型；OpenRouter 有 provider routing | ✅ |
| 将来定时 | 内置 `cronjob` 工具 | ✅ 备用 |

**边界（需自己兜底）**：
1. `hermes gateway install` 用 `schtasks ONLOGON`（+Startup 兜底）——**仍是登录级**，锁屏死因 Hermes 不解决。兜底：自动登录（netplwiz）或按官方文档建议自己用 NSSM 包服务。睡眠仍需 `powercfg`。
2. 单飞锁必须做在 skill CLI（见上）。
3. **agent 可自由修改/删除任何 skill**（自我进化特性，含后台 self-improvement）——对带安全阀的 skill 不可接受，配置 `skills.write_approval: true`（改写变暂存待审批，`/skills pending` / `/skills approve`）。真保底仍是 CLI token 门。
4. 危险命令审批默认 60s 超时即拒（fail-closed）——碎石确认不要依赖这机制，走 skill 自己的 TTL。
5. `execute_code` 在 Windows 禁用（Unix socket）——无影响，skill 走 `terminal`；别在指令里引导用它。
6. Windows 上 terminal 走 **Git Bash**——skill 命令按 bash 语法写，含空格路径注意引号。
7. Hermes 需要独立自建应用（`im:message`、`im:message:send_as_bot` 等 scope + `im.message.receive_v1` 事件订阅）；lark-cli 的应用留给其他用途。
8. Hermes 默认剥离子进程中名字含 KEY/TOKEN 的环境变量；skill 可用 `required_environment_variables` 声明透传（MAA 执行本身不需要 key，暂用不上）。

官方资料：
- 文档站 <https://hermes-agent.nousresearch.com/docs/>；仓库 <https://github.com/NousResearch/hermes-agent>
- 重点页（仓库内路径 `website/docs/`）：`user-guide/messaging/feishu.md`、`user-guide/windows-native.md`、`getting-started/platform-support.md`、`user-guide/features/skills.md`、`reference/tools-reference.md`、`user-guide/security.md`、`user-guide/features/code-execution.md`、`user-guide/features/provider-routing.md`、`user-guide/configuring-models.md`

---

## 行动清单 / 待决点

**回家先做（与架构无关）**：
1. 按议题 1 清单排查自启失效根因；
2. 启动方式换 NSSM 服务（或 schtasks ONSTART+凭据+失败重启）+ `powercfg` 禁睡眠——无论走不走 Hermes 都要做。

**决策点**：
- 是否走 Hermes 统一入口路线（讨论倾向：是）；
- 进度推送选 (a) Hermes 轮询转述 还是 (b) 脚本直发（倾向 b 或用完成通知简化，实测定）；
- 过渡期新旧并存可行（两套监听不同应用事件，不会互相误触发），Hermes 链路稳了再退役旧监听半边。

**确定要做的第一步（不依赖 Hermes 装没装）**：
- 把 executor 抽成独立 CLI：`plan`（预览+签发一次性 confirm token）/ `start --confirm-token`（锁文件单飞、后台运行）/ `status` / `stages` 四个子命令。这是 skill 的 `scripts/` 核心，也可被 Claude Code 或任何 agent 宿主驱动，不绑定 Hermes。

**小改动（若暂不迁移仍值得做）**：
- lark-cli profile 隔离（config.toml 加 `[lark].profile` + 3 调用点）；
- `.env` 加载支持。
