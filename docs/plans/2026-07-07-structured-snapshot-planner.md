# Structured Snapshot Planner v1 Plan

## Summary

实现结构化快照 Planner v1，但不新增一套平行 Planner 体系。做法是增量改造现有 Router、`task_plan.schema.json` 和提示词：每次 LLM 路由时构建 snapshot，把 MAA 能力、别名表、当前活动信息和 pending plan 放进 user prompt；LLM 输出继续走 schema 校验、validator 校验、预览确认和现有 `TaskPlan` 执行链路。

v1 目标是解决自然语言别名、当前活动关卡咨询、确认态修改丢上下文、以及 LLM 幻觉关卡/材料问题。v1 不接 Depot 仓库识别，不做“你缺某材料”类个性化库存判断。

## Key Changes

### Snapshot Builder

新增 snapshot 构建模块，作为 LLM user message 的一部分注入，不放进 system prompt，避免破坏系统提示缓存。

snapshot v1 包含：

- `maa_capabilities`
  - 可执行任务：`StartUp`、`Fight`、`Recruit`、`Infrast`、`Mall`、`Award`
  - Fight 参数：`stage`、`times`、`series`
  - 默认 `series=0`
  - `stone`、`medicine` 需要二次确认
- `aliases`
  - 代码作为唯一来源，提示词不再维护独立别名表
  - 初始别名：钱本 -> `CE-6`，经验本 -> `LS-6`，红票本 -> `AP-5`，技能书 -> `CA-5`，碳本 -> `SK-5`
- `open_activity_stages`
  - 复用本地 `StageActivityV2.json` 和 `load_open_stages()`
  - 包含活动名、关卡号、掉落、过期时间
  - 附带活动缓存文件 mtime 或读取时间
  - 只用于活动咨询、菜单渲染和 `advise_refs` 校验；不作为执行计划 `fight.stage` 的自动放行来源
- `pending_plan`
  - 若当前会话存在待确认 `TaskPlan`，序列化进 snapshot
  - 用于确认态修改，但修改输出必须是 patch，不是完整计划

### Schema And Prompt

扩展现有 `task_plan.schema.json`，不新增平行 `planner.schema.json`。

Action 集合：

- 保留现有：`run`、`ask_stage_selection`、`clarify`、`reject`
- 新增 `advise`：只输出结构化建议引用，不执行
- 新增 `approve`：确认态中表示同意执行 pending plan
- 新增 `patch`：确认态中只表达对 pending plan 的字段修改
- 使用 `allOf` 增加条件必填：
  - `action="patch"` 时必须 `required: ["patch"]`
  - `action="advise"` 时必须 `required: ["advise_refs"]`
  - 沿用现有 `clarify` 的条件必填写法，使缺载荷输出在 schema 层失败并进入现有 retry

新增字段：

- `advise_refs: string[]`
  - 仅 `action="advise"` 使用
  - 值必须是 snapshot 中存在的 stage/code
  - 用户可见回复由代码根据这些引用从 snapshot 渲染
- `patch`
  - 仅 `action="patch"` 使用
  - 结构与 `TaskPlan` 子集一致，只允许包含用户明确要修改的字段
  - patch 内 `fight.stage`、`stone`、`medicine` 等字段仍必须走 validator 和确认策略

提示词调整：

- system prompt 只描述固定规则和 schema，不塞动态 snapshot
- user prompt 包含原始用户消息和 snapshot JSON
- 确认态 prompt 明确要求输出 `approve` 或 `patch`，禁止要求 LLM 回显完整 `TaskPlan`
- 删除提示词中的硬编码别名表，改为说明“别名以 snapshot.aliases 为准”

### Router Flow

保留快速路径：

- `跑日常`
- `daily`
- `托管`
- 其他现有 fast path

非快速路径：

```text
user message
-> build snapshot
-> LLM with existing task_plan schema
-> schema validation
-> planner validator
-> RouteResult
```

确认态处理顺序：

1. 先做确定性确认/取消词判断。
2. 确认词扩展为：`确认`、`确定`、`是`、`开始`、`可以`、`好`、`行`、`没问题`、`yes`、`y`、`1`。
3. 若不是确认/取消，则走 LLM patch 路径。
4. LLM 在确认态返回 `approve` 时执行 pending plan。
5. LLM 在确认态返回 `patch` 时，代码确定性 merge 到 pending plan，并重新预览确认。
6. patch 引入 `stone > 0` 或 `medicine > 0` 时继续触发现有花费确认。
7. patch 同样必须经过 validator；validator 失败同样进入 retry。

Pending 状态互斥规则：

- 同一 `chat_id` 下，`pending_confirm` 和 `pending_selection` 不允许同时有效。
- 从确认态进入活动选择菜单时，必须先 `pop pending_confirm`。
- 原待确认计划通过 `pending_selection` 携带的 `base_plan` 保存。
- 用户随后回复 `1` 时必须优先解析为菜单选择，而不是确认旧计划。

活动选择菜单继续保留：当 LLM 输出 `ask_stage_selection` 时，仍触发现有 selection 流程。若当前存在 `base_plan`，用户选择活动关卡后只替换 `base_plan.fight.stage`，不丢失其他任务。

非确认态异常 action 处理：

- fresh route 下 LLM 输出 `approve` 或 `patch` 且没有 pending plan 时，降级为 `clarify`。

### Validator

新增轻量 validator，只做可确定校验，不校验自由文本。

`fight.stage` 执行放行集合：

- 空串，表示当前/上次关卡
- snapshot alias 表中的 stage
- 用户原始消息中显式出现的关卡 token，归一化后匹配

明确不把 `open_activity_stages` 放进执行白名单。原因：用户只说“刷当前活动代币”但没指定关卡时，必须走 `ask_stage_selection`，不能让 LLM 从活动列表里自作主张选一个关卡。活动列表只用于 `advise` 和菜单。

关卡 token 归一化规则：

- 全角半角统一
- 大小写统一
- 去掉分隔符后比对，例如 `ce6`、`CE6`、`CE-6` 可匹配
- 对 `OF-F4` 这类关卡，两边都去掉 `-` 后比对，例如 `off4` 匹配 `OF-F4`
- 匹配成功后保留 LLM 输出的原形或现有规范 stage，不尝试从 token 生成新关卡号
- 正则只做 token 提取辅助，不作为单独放行条件

`advise` 规则：

- LLM 只输出 `advise_refs`，不输出最终自然语言回复
- validator 检查 `advise_refs` 必须存在于 snapshot 的 aliases 或 open activity stages
- 用户可见回复由代码从 snapshot 确定性渲染，避免自由文本幻觉
- 无活动数据时，活动类 `advise` 返回明确降级回复，不编造活动材料

validator failure 必须参与 retry：

- validator 返回明确错误原因
- Router 将错误原因回灌进下一次 LLM user prompt
- 重试耗尽后返回澄清，不执行

validator 接口必须显式接收原始 `msg.text`：

- “用户原始消息中显式出现的关卡 token”只能从原始 `msg.text` 判断
- 不允许从拼装后的 LLM prompt 中提取 token
- 原因：确认态 prompt 会包含 pending plan JSON；若从拼装 prompt 提取，旧 `fight.stage` 会被误判为用户本轮说过
- 推荐接口形态：`validate_planner_output(plan_data, snapshot, original_text, mode)`

## Required Scenarios

- 用户：“跑日常，刷钱本”
  - 输出整套日常
  - `fight.stage = CE-6`
- 用户：“跑日常”
  - 系统进入确认态
  - 用户：“可以，但是把刷理智换成钱本”
  - 输出 `patch`
  - 只改 `fight.stage = CE-6`
  - 保留公招、基建、信用、领奖
- 用户：“跑日常”
  - 系统进入确认态
  - 用户：“可以”
  - 直接执行，不进入修改循环
- 用户：“跑日常”
  - 系统进入确认态
  - 用户：“换个活动关吧”
  - LLM 输出 `ask_stage_selection`
  - Router 清掉 `pending_confirm`，设置携带 `base_plan` 的 `pending_selection`
  - 用户回复 `1`
  - 必须解析为活动菜单第 1 项，并用该关卡替换 `base_plan.fight.stage`
- 用户：“当前活动能刷什么”
  - 返回 `advise`
  - 不执行 MAA
  - 回复基于本地活动缓存，并标注数据来源时间
- 用户：“刷当前活动代币”
  - 如果没有明确关卡，进入现有关卡选择菜单
- 用户：“打 OF-F4 三次”
  - 即使 `OF-F4` 不在 snapshot，只要用户原文显式出现且归一化匹配，允许执行
- LLM 幻觉 `SN-10`，但用户原文没有提到且 snapshot alias 不包含
  - validator 拒绝并重试；最终仍失败则澄清或拒绝

## Test Plan

### Snapshot Tests

- 默认能力清单包含现有 MAA 子任务和 Fight 参数
- alias 表能解析钱本、经验本、红票本、技能书、碳本
- alias 表是代码唯一来源，提示词不包含独立别名表
- activity snapshot 正确过滤当前开放关卡
- activity snapshot 包含缓存 mtime 或读取时间
- pending plan 存在时被序列化进 snapshot

### Validator Tests

- alias 关卡允许执行
- open activity stage 不因“存在于活动列表”而直接允许执行
- 空串关卡允许执行
- 用户手输 `OF-F4` 这类不在 snapshot 的关卡允许执行
- pending plan JSON 中出现旧关卡、但原始 `msg.text` 未提到该关卡时，不能通过“用户原文出现”分支
- `ce6`、`CE6`、`CE-6` 能归一化匹配
- `OF-F4`、`OFF4` 能通过去分隔符策略匹配
- LLM 幻觉格式合法关卡但用户没说过、alias 也没有时失败
- `advise_refs` 引用不存在的 stage/code 时失败
- `action="patch"` 缺少 `patch` 时 schema 失败并触发 retry
- `action="advise"` 缺少 `advise_refs` 时 schema 失败并触发 retry
- 无活动数据时的 `advise` 返回明确降级回复，不编造活动材料

### Router Tests

- “跑日常，刷钱本”生成整套日常，关卡为 `CE-6`
- pending plan 修改只应用 `patch` 字段
- pending plan 纯“可以/好/行”直接执行，不循环预览
- pending plan patch 引入 `stone` 或 `medicine` 时重新触发花费确认
- pending confirm -> `ask_stage_selection` -> 回复 `1` 时选择菜单第 1 项，不执行旧 confirm 计划
- fresh route 下收到 `approve` 或 `patch` 时降级澄清
- “当前活动能刷什么”只建议不执行
- “刷当前活动代币”进入活动选择菜单
- LLM 非法 JSON、schema violation、validator failure 均可重试并最终澄清

### Eval And Full Verification

- 更新 `evals/router_cases.jsonl`，覆盖 alias、活动建议、pending plan patch、approve、活动菜单选择、幻觉关卡拒绝、超出 MAA 能力拒绝
- 跑现有 eval：

```powershell
.\.venv\Scripts\python -m maa_remote.eval_router
```

- 跑全量测试：

```powershell
.\.venv\Scripts\python -m pytest tests -q
```

## Assumptions

- v1 不接 Depot 仓库识别。
- v1 不输出“你缺某材料”这种库存判断。
- alias 表先写在代码中并配测试；未来需要用户自定义时再迁配置。
- `advise` 永不执行。
- 活动数据来源只使用本地 `StageActivityV2.json`。
- 不在每条消息前执行 `maa hot-update`；是否热更新仍沿用现有 `hot_update_before_catalog` 的菜单路径行为。
- 活动 advise 允许基于本地缓存，必须在回复中体现缓存时间，避免把陈旧数据伪装成实时数据。
- 选择菜单挂起时，用户发非编号/非关卡文本会继续走现有“没听懂，回复编号或关卡号”行为；允许落回 fresh route 是后续 UX 优化，不纳入 v1。
