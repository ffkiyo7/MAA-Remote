# MAA_remote —— 设计上下文与交接文档

> 状态：**设计定稿（2026-07-04 评审修订版）**（架构 + 第 2 段模块接口已完成，详见 `SPEC.md`；实现计划 `docs/plans/2026-07-04-maa-remote.md`）
> 最后更新：2026-07-04
> 换电脑续聊：打开本项目，让 Claude Code 读 `CONTEXT.md` + `SPEC.md`，说"接着上次的设计往下讲"即可。
> ⚠️ 依赖/登录态是**每台机器各不相同**的：换机后先重扫本机环境（见 §五 / `SPEC.md §0`），别照抄。

---

## 一、目标（这个项目要做什么）

在飞书/IM 里对机器人说一句话（如"跑一下 MAA 日常"），本地 Agent 就用 **maa-cli**（不碰 MAA GUI）跑明日方舟日常，跑完把结果润色成自然语言推回飞书。使用场景：**电脑开着、单用户、按需触发**。

---

## 二、已确定的决策

| 主题 | 结论 |
|---|---|
| 执行方式 | 走 **maa-cli**，不操作 GUI |
| Agent 智能度 | **混合**：关键词快速路径 + 自定义自然语言请求走 LLM 解析 |
| LLM 供应商 | **DeepSeek**（成本敏感，目标 < ¥10/月；实测混合方案 < ¥1/月，忽略不计。省钱关键：缓存固定的系统提示）。2026-07-05 定：模型切 `deepseek-v4-flash`（官方 **2026-07-24 下线** deepseek-chat/reasoner，必须迁），thinking 默认 enabled、reasoning_effort=high，均走 `config.toml` 可配（计划：`docs/plans/2026-07-05-llm-v4-flash-thinking.md`） |
| 运行设备 | **安卓模拟器，需自动拉起**（平时不开）。工作流负责：拉起模拟器 → 等 adb 就绪 → 跑 MAA → 结束后可选关闭 |
| 日常任务范围 | 开局唤醒+收邮件 · 刷理智(fight) · 公招+信用商店 · 基建使用游戏内一键换班+领日常奖励；**再叠加**自然语言临时指定关卡/操作 |
| 刷理智默认（快速路径） | **"揉揉乐"**：`expiring_medicine: true`（只吃即将过期的理智药）+ `medicine: 0`（不动囤药）+ **不碎石**。零成本清库存 |
| 汇报形式 | **LLM 润色的自然语言总结**（含异常提醒） |
| 触发方式 | **仅按需（IM 触发）**，不做每日定时。只需一个常驻监听，无需定时器 |
| 飞书身份 | **每机不同**。运行时读 `auth status` 取当机身份，不写死；解析不到 → **启动报错退出**（绝不带空过滤器静默运行）。（本机 2026-07-02：bot 身份，appId `cli_a9429c3b63f89cc1`；user token 过期，仅 bot 可用） |
| 换飞书组织/账号 | 支持且零改代码：`lark-cli config init`（新组织 app-id/secret）+ `auth login`。想用**私人组织 bot 避免真名**——bot 是应用无真名，只有登录用户带名，换私人组织账号登录即可 |
| 活动关卡 | 不写死。StageCatalog 读 maa-cli 的 `StageActivityV2.json`，LLM 列菜单给你选（应对 SideStory 新关卡） |
| 意图识别 | 全 LLM 优先 + 子任务独立开关；质量靠**提示词契约三件套**（prompts/schema/evals）约束，见 `SPEC.md §5` |
| 碎石/囤药安全阀 | plan 含 `stone>0` 或 `medicine>0` 时**不直接执行**，先回确认问句，用户回「确认」才放行（TTL 过期作废）。防 LLM 误解析烧钱 |
| StartUp 恒开 | 任何执行计划 `startup` 恒为 true——模拟器平时关着，冷启动后游戏未开，没 StartUp 后续任务全挂；StartUp 幂等，已在游戏内时秒过 |

### 名词解释：什么是"揉揉乐"
社区玩梗。对应 MAA 的 `expiring_medicine`：每天把库存里快过期的应急理智/理智药顺手用掉（像揉面团一样随手揉掉），避免白白过期；但不动还早的囤药，也不花源石碎石。稳赚不亏。

---

## 三、架构（方案 A：单进程常驻服务）—— 已确认

对比过的三种方案：
- **A 单进程常驻服务**（选定）：一套代码一个进程，最简单最稳。
- B 监听+队列+Worker：worker = 独立后台执行进程，从队列取任务跑 maa。能排队/重试，但单用户过度设计。
- C Claude Code headless 当大脑：推理强但成本走 Claude，和预算冲突，更重。

### 数据流

```
飞书(你 DM 机器人)
   │  im.message.receive_v1
   ▼
① 监听 Listener ── 子进程 `lark-cli event consume` 读 NDJSON
   │  过滤：只认「你本人 / 指定会话」发来的文本
   ▼
② 路由 Router
   ├─ 快速路径：命中关键词("跑日常"/"日常"/"daily") → 预设任务表
   └─ LLM 路径：其余文本 → DeepSeek 解析成结构化任务 JSON(带 schema 校验)
   ├─ 立刻回「🟡 收到，开始跑日常…」
   ▼
③ 执行 Executor
   ├─ 拉起模拟器 → 轮询 adb 直到 device ready(超时兜底)
   ├─ 渲染 maa-cli 配置/参数 → 子进程跑 maa
   └─ 实时收集 stdout/stderr + 退出码
   ▼
④ 汇报 Reporter
   ├─ 从 maa 日志解析关键数据(理智/公招/信用/基建/异常)
   ├─ DeepSeek 润色成自然语言总结
   └─ lark-cli 回复到触发消息所在会话
```

关键约束：
- **一次只跑一个任务**：执行放 **worker 线程**（监听主线程不被阻塞），执行中再来消息 → 立刻回"正在跑中，稍等"，绝不并发（防两个 maa 抢模拟器）。注意：若执行和监听同线程串行，busy 回复是死代码、积压消息跑完后会被当新指令连跑——这是 2026-07-04 评审修掉的坑。
- **旧消息不执行**：距今超过 `max_msg_age_s`（默认 5 分钟）的消息直接丢弃，防服务重启后积压事件轰炸。
- **每步都有兜底**：模拟器起不来 / adb 超时 / maa 非零退出 / DeepSeek 超时 / worker 崩溃 → 都回明确失败消息，不静默。
- **碎石/囤药先确认**：见 §二 决策表"安全阀"行。

---

## 四、设计原则：不写死身份/配置（重要）

不同电脑登录的飞书账号可能不同，因此：
- **不硬编码** appId / bot-or-user / chat_id。运行时读 `lark-cli auth status`，用 `--as auto` 采用当机身份。
- **回复目标动态解析**：回给触发消息所在会话（从事件取 `chat_id`/`sender`），不写死群/人。
- **DeepSeek key、模拟器路径、adb 端口、MAA 关卡默认值** 全走 `config.toml` + 环境变量，不进代码。

---

## 五、环境现状（本机已探测）

> ⚠️ 下表是**本机（E: 这台）2026-07-02 实测**，不代表别的机器。换机重扫。完整版见 `SPEC.md §0`。

| 依赖 | 本机状态 |
|---|---|
| Python | 3.14.3 ✅ |
| Node | v24.14.0 ✅ |
| lark-cli | **1.0.63** ✅（已从 1.0.0 更新；npm 包 `@larksuite/cli`）。收消息命令：`lark-cli event consume im.message.receive_v1`（注意随版本变过：1.0.0 曾是 `event +subscribe`） |
| maa-cli | **v0.7.5** ✅（已装，`%LOCALAPPDATA%\Programs\maa-cli\maa.exe`；`maa activity` 关卡能力已核实可用） |
| MaaCore | maa-cli 自身未装 core，**复用 MAA GUI 的** `MaaCore.dll`+resource |
| MAA GUI | **已更新到 v6.13.0**（目录 `D:\Desktop\MAA-v5.20.0-beta.3-win-x64`，名是旧的属误称） |
| 模拟器 | **MuMu 12 明日方舟定制版** ✅（`D:\Program Files\YXArkNights-12.0`，实例0；启停走 `MuMuManager.exe control -v 0 launch/shutdown`）|
| adb | ✅ `...\YXArkNights-12.0\shell\adb.exe`，端口 **16384**（serial `127.0.0.1:16384`）|
| 连接方式 | **仅模拟器(ADB)**。maa-cli 不支持 PC 客户端 Win32 控制（那是 GUI 独有 beta），已核实 |

---

## 六、待办 / 下次续聊要展开的部分

> ✅ 第 2 段设计已完成 → 见 `SPEC.md`（模块接口 + config.toml + 提示词契约 + 错误处理矩阵）。
> ✅ 错误处理与超时策略 → `SPEC.md §6`。

1. ~~第 2 段设计~~ ✅ 见 `SPEC.md`。
2. ~~错误处理与超时~~ ✅ 见 `SPEC.md §6`。
3. ~~测试策略~~ ✅ 计划内 TDD 全覆盖 + `python -m maa_remote.eval_router` 跑意图识别回归。
4. 落地时需向你采集的信息：
   - ~~模拟器路径/端口~~ ✅ 已入 `config.toml`。
   - DeepSeek API key（环境变量 `DEEPSEEK_API_KEY`）。
   - 飞书事件订阅是否已开启（`im.message.receive_v1`）、机器人 IM 权限范围。
   - `lark-cli auth status` 在 bot-only 态下是否返回 `userOpenId`（不返回则 config 显式填 open_id）。
5. ✅ 实现计划（2026-07-04 评审修订版）→ `docs/plans/2026-07-04-maa-remote.md`（12 个 TDD 任务 + 运维 + 端到端冒烟清单）。下一步：照计划逐任务执行。

---

## 七、如何在新电脑上续聊

1. 确保本项目目录（含本文件）在新电脑可访问。
2. 在项目里启动 Claude Code，说："读 `CONTEXT.md` + `SPEC.md`，接着 MAA_remote 往下做。"
3. 注意：Claude Code 的项目记忆存在本机 `~/.claude/projects/<路径 slug>/memory/`（**不随项目目录走**）。本文件第四节已把最关键的"不写死身份"约束复制进来，所以换机也不丢。
