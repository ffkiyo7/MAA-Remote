# MAA_remote 增量设计:进度推送 + 计划确认 + 换专用应用

> 状态:**设计定稿(2026-07-05,用户已确认)**
> 前置:基础服务已跑通(飞书触发 → 模拟器 → maa-cli 日常 → 润色总结回飞书),见 `CONTEXT.md` / `SPEC.md`。
> 实现分工:**Codex 写码,Claude review**。实现计划见 `docs/plans/2026-07-05-progress-and-confirm.md`。

---

## 一、需求与已拍板的决策

| # | 需求 | 用户决策 |
|---|---|---|
| 1 | 无 GUI 时看不到 MAA 执行进度,要在飞书里呈现每个模块(公招/基建/作战…)的进度 | **话题内推送**:主聊天只有「开始」和「最终总结」两条,过程细节发在开始消息的话题(thread)里 |
| 2 | 每次跑日常前先预告本次会执行哪些操作,用户确认后再开始 | **每次都确认**:先发计划预告,回「1/确认」才跑,10 分钟不回作废;说「直接跑日常」可跳过确认 |
| 3 | 把自建 bot 从 lark-cli 授权时绑定的 CLI 应用换成专用命名的自建应用 | 纯运维操作、零代码,放在功能开发完成之后做(见 §七) |

### 预期对齐(已向用户说明)

- 「这次会清多少体力」**无法精确预告**——不打开游戏,MAA 不知道当前理智/过期药存量。预告是**策略级**(刷哪关、吃不吃过期药、上不上限),精确数字在跑完的总结里。
- 飞书**单聊里话题(thread)的通知体验**未实测。保险丝:进度样式做成配置项 `progress.style = "thread" | "flat"`,体验不好时改配置即可退回分条推送,零代码回退。

---

## 二、关键技术事实(已核实)

- MaaCore 输出结构化事件:`TaskChainStart` / `TaskChainCompleted {"taskchain":"Award",...}` 等,taskchain 名恰好对应模块(StartUp/Recruit/Infrast/Mall/Award/Fight)。GUI 的进度显示即来源于此。
- lark-cli 1.0.63 `im +messages-reply` 支持 `--reply-in-thread`(消息进入话题流)与 `--markdown`;发送成功的 JSON 输出中含新消息的 `message_id`(锚点消息用)。
- lark-cli **不支持编辑已发消息**(`im messages` 资源无 update),故「单条消息原地更新」方案排除,列为可选二期(需原生 OpenAPI)。
- 当前 `executor.run_maa` 用阻塞式 `run_utf8`,跑完才拿到输出——流式改造是本次核心改动。
- maa-cli `--batch` 模式 stdout 的确切行格式**未定稿**,需实跑一次抓样本(实现计划的 Task 0);若 stdout 不含 taskchain 级信号,备选方案是并行 tail MaaCore 的 `asst.log`。

---

## 三、交互流程(最终形态)

```
你:跑日常
bot:(回复)📋 本次计划
     ① 开游戏 & 收邮件
     ② 公招:最多 4 次(自动选高星词条,不加急)
     ③ 基建:游戏内一键换班
     ④ 信用商店:优先买招聘许可、龙门币(不买碳/家具)
     ⑤ 领日常任务奖励
     ⑥ 刷理智:1-7,只吃快过期的药,不动囤药,不碎石
     回「1」或「确认」开始;回「取消」作废;10 分钟不回自动作废。
你:1
bot:🚀 已开始跑日常,过程进度在本条消息的话题里    ← 主聊天第 1 条(锚点)
     └(话题)🖥️ 拉起模拟器中…
     └(话题)✅ 模拟器就绪 → 🎮 启动游戏中…
     └(话题)✅ 游戏已进入 → 🎫 公招中…
     └(话题)✅ 公招完成 → 🏗️ 基建换班中…
     └(话题)…
bot:✅ 跑完了。公招 3 次,基建已换班,1-7 打了 6 次…  ← 主聊天第 2 条(润色总结,现状保留)
```

- 「直接跑日常」→ 跳过确认,直接出锚点消息开跑。
- 中途失败:话题里发 ❌(含卡在哪个模块),主聊天总结说明失败原因(现有失败汇报保留)。
- 确认等待期收到**新任务指令**(非确认/取消词)→ 旧待确认计划作废,按新指令重新路由、重新预告。

---

## 四、模块设计

### 4.1 新增 `maa_remote/preview.py` —— 计划预告

- `plan_preview(plan: TaskPlan, cfg: Config) -> str`
- **同源原则**:预告文案从 `executor.build_task_file(plan, client)` 的产物(tasks 数组)渲染,而非从 plan 另写一套逻辑——保证"说的"和"跑的"永远一致。
- 含碎石/囤药时在预告中用 ⚠️ 突出(沿用现有安全阀话术)。

### 4.2 新增 `maa_remote/progress.py` —— 进度事件与推送

- `ProgressEvent(kind, chain, detail)`:kind ∈ `emulator` | `chain_start` | `chain_complete` | `chain_error` | `info`。
- `parse_progress_line(line: str) -> ProgressEvent | None`:解析规则由 Task 0 抓取的真实样本锁定(fixture 驱动测试)。
- `TASKCHAIN_LABELS`:StartUp→🎮 启动游戏、Recruit→🎫 公招、Infrast→🏗️ 基建换班、Mall→🛒 信用商店、Award→🎁 领奖励、Fight→⚔️ 刷理智。
- `ProgressSender(anchor_message_id, identity, style, runner)`:
  - 合并展示:上一个 chain 的「✅ X完成」缓冲到下一个「Y中…」一起发(`✅ 公招完成 → 🏗️ 基建换班中…`),结束时冲刷缓冲。
  - style=`thread`:`+messages-reply --message-id <锚点> --reply-in-thread`;style=`flat`:普通回复触发消息。
  - **推送失败绝不影响执行**:捕获一切异常,只记日志。

### 4.3 改造 `maa_remote/executor.py` —— 流式执行

- `run_maa` 改用 `subprocess.Popen`(`encoding="utf-8", errors="replace"`,行缓冲)逐行读 stdout(stderr 合流),每行尝试 `parse_progress_line`,命中则回调 `on_event`;全部行仍累积为 `raw_log`,结尾 `parse_maa_log` 提取 facts(现状保留)。
- 超时用 `threading.Timer` 看门狗到点 `proc.kill()`,正常退出时取消;被杀 → 返回「MAA 超时」失败。
- `ensure_emulator` / `execute` 增加 `on_event` 回调(拉起模拟器中/模拟器就绪);`on_event=None` 时行为与现状完全一致。
- 测试注入点:`popen` 参数可注入假进程(脚本化输出行)。

### 4.4 改造 `maa_remote/router.py` —— 确认状态机推广

- 现有碎石/囤药确认推广为全量确认:`_maybe_confirm` 规则——
  1. 计划含花费(stone>0 或 medicine>0)→ **无条件确认**(不受跳过词、不受 mode 影响,省钱红线保留);
  2. 否则 `confirm.mode == "always"` 且未命中跳过词 → 预告 + 确认;
  3. 否则直接执行。
- 确认词扩为 `{"确认","确定","是","yes","y","1","开始"}`;取消词不变。
- 等待确认期间收到其他文本 → 作废旧计划,**按新指令走完整路由**(不再只是复读提示)。
- 跳过词:文本以「直接」开头且去掉「直接」后命中 FAST_PATH(如「直接跑日常」)。

### 4.5 改造 `maa_remote/reporter.py` + `__main__.py` —— 锚点与接线

- `send_reply` 返回发出消息的 `message_id`(解析 lark-cli JSON 输出;解析失败返回 None)。
- `handle_message` 执行分支:发锚点消息(即原 ack 位置,文案改为「🚀 已开始…进度在本条话题里」)→ 拿锚点 message_id 构造 `ProgressSender` → 作为 `on_event` 传入 `execute`。锚点 id 拿不到 → 降级 flat 或禁用进度(记日志),任务照常跑。
- 最终总结:现状保留(润色后回复触发消息)。

### 4.6 配置新增(`config.toml` / `config.example.toml`)

```toml
[progress]
enable = true
style = "thread"     # thread | flat(话题体验不好时的零代码回退)

[confirm]
mode = "always"      # always | spend_only(回到只对花钱计划确认)
ttl_s = 600
```

- 缺省兼容:节缺失时取上述默认值,老配置文件不炸。

---

## 五、边界与错误处理

| 场景 | 行为 |
|---|---|
| 服务重启丢失待确认计划 | 用户回「1」→ 无 pending → 走正常路由("1"不命中任何意图 → LLM 兜底/澄清) |
| 进度信号解析不到(MAA 改日志格式) | 任务照常跑,话题里发一条「本次拿不到细粒度进度,请等最终总结」 |
| lark-cli 推送进度失败 | 捕获 + 记日志,继续执行,绝不中断任务 |
| 锚点消息发送失败 | 降级:进度直接回复触发消息(flat)或禁用,记日志 |
| MAA 超时 | 看门狗杀进程,失败汇报注明超时 |
| 确认等待不占执行锁 | 预告只是 reply,锁仍只在真正执行时获取 |

---

## 六、测试策略

- `progress` 解析:真实抓取样本(fixture)驱动;`ProgressSender` 合并/冲刷/失败吞异常各一测。
- `router` 确认机:确认/取消/超时/新指令覆盖/花费计划无条件确认/跳过词 六条路。
- `preview`:典型日常计划与自定义计划的文案快照测试。
- `executor` 流式:假 popen 脚本化输出 → 事件顺序断言;看门狗超时杀进程路径。
- 端到端:实跑一次日常,人工验收话题通知体验(决定 style 默认值是否保留 thread)。

---

## 七、换专用自建应用(运维清单,零代码,最后做)

1. 飞书开放平台建新自建应用(命名如「明日方舟日常助手」),开启**机器人**能力;
2. 权限:IM 接收/发送消息;事件订阅 `im.message.receive_v1`,**长连接模式**;发布版本,可用范围包含本人;
3. 本机 `lark-cli config init`(新 app_id/app_secret)→ `lark-cli auth login`;
4. 给新 bot 发一条消息激活会话;重启 MAA-remote 服务。

⚠️ 注意:`open_id` 按应用隔离,换应用后同一用户 open_id 会变。代码运行时自动解析,但需确认 `config.toml` 的 `[lark].allowed_sender_open_id` 为空(自动模式)或更新为新值。**只给 bot token 不可行**——长连接与令牌刷新需要 app_id+secret。

---

## 八、明确不做(YAGNI)

- 单条消息原地更新(需原生 OpenAPI 编辑消息,列为可能的二期);
- 子任务(SubTask)粒度进度(太噪,taskchain 粒度足够);
- 交互式卡片按钮确认(文本「1/确认」足够);
- 多用户/群聊场景(维持单用户 DM)。
