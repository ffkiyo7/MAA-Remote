# MAA_remote 增量设计：飞书抄作业打活动关（prts.plus × maa copilot）

> 状态：**设计草案（2026-07-07 对齐；2026-07-08 跑了首批 Spike）**——核心可行性已坐实：**P0(`maa run`+Copilot 组链)/S1-API/S5 ✅ 通过**；`spikes/copilot_catalog.py` 已完成 prts 查询→硬过滤→软打分的 headless 原型并留存 fixtures；**S2 已定案：copilot_list 不做"主页→地图",需自建导航 override**；S3/S4 未跑。详见 **§十一 Spike 执行记录** 和 `spikes/SPIKE_REPORT.md`。下一步：把 spike 原型产品化到 `maa_remote/copilot_catalog.py`，并补 S2 导航 / S4 练度来源。
> 前置：基础服务已跑通（飞书触发 → 模拟器 → maa-cli 日常 → 润色总结回飞书），见 `CONTEXT.md` / `SPEC.md`；进度推送+计划确认见 `2026-07-05-progress-and-confirm-design.md`。
> 触发场景：DM「帮我抄作业打一下新的活动关」→ 自动从 prts.plus 拉作业 → maa-cli 自动战斗 → 逐步确认 → 汇报。

---

## 一、需求与已拍板的决策

| # | 问题 | 用户决策（2026-07-07） |
|---|---|---|
| 1 | 练度不可知：不知道账号有哪些干员、精英化/技能等级是否满足作业要求 | 数据源分层：**OperBox（MAA 干员识别）为基线**；森空岛 API 因参考仓库已 archive 两年多、可靠性未知，**降级为 Spike（S3）**，跑通才用 |
| 2 | 自动战斗要求从「开始行动」编队界面开始，全自动化没有固定路径到达 | 用**战斗列表模式**（起点放宽到活动地图界面）；剩余缺口「主界面→活动地图」列 Spike（S2），不通则自写 OCR 导航 override |
| 3 | 战斗失败/非三星后怎么办 | **绝不自动重跑**。失败即停 → 报告失败详情 → 给备胎作业清单 → 用户确认换哪份 / 跳过该关 / 取消，每一步都要确认 |
| 4 | 资源消耗 | 延续安全阀：不碎石、不动囤药、`use_sanity_potion=false`；单次翻车损失上限 = 一关理智 |

---

## 二、关键技术事实（2026-07-07 已核实，附来源）

### 2.1 MAA 自动战斗（Copilot）

- **单作业模式**：手册原文「本功能需要在有`开始行动`按钮的编队选择界面开始运行」。
- **战斗列表模式**：手册原文「开启本功能后改为在**关卡所在的地图界面**开始自动战斗」；限制「请确保列表中的关卡在同一区域（只通过左右滑动地图界面就可以导航到）」；「在理智不足/战斗失败/非三星结算时将停止自动战斗队列」——**失败即停是官方行为，恰好配合决策 3**。
- **自动编队**「会**清空当前编队**并根据作业需要的干员自动完成编队」→ 用 `formation_index` 指到备用编队位，不碰手动维护的队伍。
- **MAA 不会借干员**（手册原文；`support_unit_usage` 参数存在但 [issue #16049](https://github.com/MaaAssistantArknights/MaaAssistantArknights/issues/16049) 反映逻辑过时）→ **匹配阶段必须"全干员自有"**。
- 来源：[自动战斗手册](https://docs.maa.plus/zh-cn/manual/introduction/copilot.html)。

### 2.2 集成协议 Copilot 任务参数

`filename`（单作业）/ `copilot_list`（数组，每项 `filename` + `stage_name` + `is_raid`，驱动地图内自动导航切关）/ `formation` / `formation_index`（0-4，0=当前队）/ `add_trust` / `ignore_requirements` / `user_additional` / `use_sanity_potion`（默认 false）/ `loop_times`。
来源：[集成协议](https://docs.maa.plus/zh-cn/protocol/integration.html)。

### 2.3 maa-cli copilot 子命令（源码已读）

- 多 URI → 自动组 `copilot_list`；flags：`--formation` `--formation-index` `--add-trust` `--ignore-requirements` `--use-sanity-potion` `--raid` `--loop-times`。
- 下载地址：单作业 `https://prts.maa.plus/copilot/get/{code}`，作业集 `/set/get?id={code}`。
- ⚠️ 源码里存在交互式确认（`Please set up your formation manually` + BoolInput）→ **`--batch` 下是否全免交互需实测（S5）**。
- 来源：[maa-cli copilot.rs](https://github.com/MaaAssistantArknights/maa-cli/blob/main/crates/maa-cli/src/run/preset/copilot.rs)。

### 2.4 OperBox 干员识别回调

- `all_oper[]`：`id` / `name` / `own` / `rarity`（全干员，含未拥有）。
- `own_opers[]`：`id` / `name` / `own` / `elite`（精英化）/ `level` / `potential` / `rarity`。
- **没有**：技能等级、专精（M1-3）、模组。
- 来源：[回调协议](https://docs.maa.plus/zh-cn/protocol/callback-schema.html)。

### 2.5 prts.plus 后端 API（2026-07-07 线上实测）

- `GET https://prts.maa.plus/copilot/query?page=&limit=&orderBy=hot` → `{status_code, data:{total, has_next, page, data:[{id, upload_time, uploader, views, hot_score, like, dislike, content}]}}`；`content` 是**作业 JSON 字符串**（查询结果已含全文，匹配阶段无需逐条 get）。
- ⚠️ 关卡过滤参数名未定：`levelKeyword=1-7` 实测**疑似未生效**（total=38482 ≈ 全量，首条是危机合约作业）；响应体是 snake_case → 大概率是 `level_keyword`，**并入 S1 用 curl 定案**。
- `GET https://prts.maa.plus/arknights/level` → 全关卡表 `{level_id, stage_id, cat_one:"活动关卡", cat_two:活动名, cat_three:关卡号(如 "FC-EX-2"), name}`（关卡号 ↔ level_id 映射，用于校验查询结果对了关）。
- API 与 maa-cli 同源（maa-cli 就在用 `/copilot/get`），稳定性风险低。

### 2.6 作业 content JSON（[作业协议](https://docs.maa.plus/zh-cn/protocol/copilot-schema.html)）

- `stage_name`（level_id）、`opers[]{name, skill(1-3), skill_usage, requirements?{elite, level, skill_level, module, potentiality}}`、`groups[]`（**可替换干员组，任选其一即可编队**——练度匹配的最大缓冲）、`doc`（自由文本，作者常写"低配/三级技能可用/需专精"）、`difficulty`。
- `requirements` 是可选字段，大量作业不填 → 需要 doc 文本兜底解析（DeepSeek，已有 LLM 管线）。

### 2.7 森空岛 API（未核实可用性 → S3）

- 端点：`zonai.skland.com/api/v1/game/player/info`（token → cred → 签名请求）；声称返回完整 Box：干员精英化/等级/**每个技能的专精等级 specializeLevel**/模组。
- ⚠️ [Skland_API 文档仓](https://github.com/ProbiusOfficial/Skland_API) 已 archive 两年多；以 [arknights-mower 的 skland 模块](https://github.com/ArkMowers/arknights-mower/blob/main/%E6%98%8E%E6%97%A5%E6%96%B9%E8%88%9F%E6%A3%AE%E7%A9%BA%E5%B2%9B%E6%95%B0%E6%8D%AE.py)（项目仍活跃）为主要参考。**能跑通才纳入，跑不通就 OperBox-only（§三已论证够用）。**

---

## 三、OperBox 兜底能到什么程度（练度数据源分层）

| 判定项 | OperBox 能否 | 说明 |
|---|---|---|
| 是否拥有该干员 | ✅ `all_oper.own` | 淘汰"缺干员"的作业——自动编队必挂的第一死因 |
| 精英化 / 等级 | ✅ `own_opers.elite/level` | 直接比对 `requirements.elite/level`；另外作业 `opers[].skill` 隐含精英要求（2 技能需精一、3 技能需精二），可推断校验 |
| 潜能 | ✅ `own_opers.potential` | 少数作业有要求 |
| 技能等级 / 专精（M1-3） | ❌ 盲区 | 影响数值，可能"编得出但打不过" |
| 模组 | ❌ 盲区 | 同上 |

**结论：OperBox 足以保证"作业能开起来"——缺人/精英不足这两类硬失败在开打前就能完全排除**（MAA 自动编队自身还会校验 `requirements`，编不出会在消耗理智前失败，双保险）。**不能保证"打得过"**（专精/模组盲区）。盲区的缓解按序：

1. 作业有填 `requirements.skill_level` → 无本地数据时按"有风险"标注，进确认文案；
2. DeepSeek 解析 `doc` 文本：「低配 / 无专精 / 三级技能」加分，「满专 / M3 / 高配速通」降权并标注风险；
3. 事前确认 + 失败即停确认（§六）——最坏损失一关理智，用户始终在环。

森空岛若 S3 跑通，把两个盲区全补上，且刷新练度不用开模拟器；OperBox 刷新需要拉起模拟器跑一次识别。

**练度缓存**：`roster.json`（记录来源 `operbox|skland` + 抓取时间）；飞书指令「更新练度」触发刷新；匹配时缓存超过 N 天提示"练度数据是 X 天前的"。

---

## 四、作业获取与匹配

1. **定位关卡**：复用 `stage_catalog`（`StageActivityV2.json`）拿当期活动名/关卡号列表（"新的活动关"→ 现有 `ask_stage_selection` 交互或 `scope=all_new` 全选）；用 `/arknights/level` 把关卡号映射到 `level_id`，校验查询结果没串关。
2. **查询**：`/copilot/query` 按关卡过滤 + `orderBy=hot`，取前 N（默认 10）份，直接解析响应里的 `content`。
3. **硬过滤**：`opers` + `groups` 每个槽位都能被自有干员满足（groups 任一成员即可）；精英化/等级达标（含 skill→精英隐含要求）。**不满足直接淘汰，绝不指望借助战。**
4. **软打分排序**：热度/浏览量 + `requirements` 满足裕度 + doc 文本信号（LLM）+ 上传时间（新活动首日 hot 未积累，兼顾时间新鲜度）。
5. **产出**：排序后的候选清单（作业 id / 标题 / 干员需求逐项 ✅⚠️ / 风险点），**全部进确认流程（§六），代码不自动定案**。

---

## 五、执行链路与导航缺口

```
StartUp（拉起游戏、登录、到主界面 —— 已有，恒开）
→ [缺口] 主界面 → 当期活动的地图界面        ← Spike S2
→ Copilot(copilot_list=[...] 或单关 filename,
          formation=true, formation_index=<备用编队位>,
          is_raid=按关, use_sanity_potion=false)
→ MAA 地图内自动导航切关、自动编队、开打；失败/非三星自动停
→ Reporter 逐关解析结果 → §六 确认流程
```

缺口三方案（按序尝试）：

1. **S2 实测**：本机 GUI v6.13 战斗列表从主界面点开始，看新版是否已能自动导航进活动（文档偏保守，导航 OCR 近期一直在迭代）；行 → 核实 CLI 同参数同行为。
2. **自写导航 override**：maa-cli 支持用户自定义资源；用任务流程协议写「主界面 → 终端（固定位置）→ OCR 点击活动名文字 → 活动地图」。活动名 `stage_catalog` 已有，OCR 点文字对每期活动通用，一次投入长期免维护。
3. **兜底**：导航失败 → 明确报错 + 截图回飞书（已有 preview 能力），绝不静默。

注意事项：
- **活动首日必须先 hot-update**（导航数据/新干员模板没更新 → 编队识别不出新干员、导航找不到新关）；复用 `hot_update_before_catalog` 挂到 copilot 流程前。
- 战斗列表要求**同一地图区域**；跨区域（如普通关 vs EX 关分屏）拆成多次任务执行。
- 作业 JSON 由我们自己从查询结果落盘到本地（`<config_dir>/copilot/` 之类），`filename` 传本地路径——匹配阶段已有全文，无需二次下载；执行走**自定义任务文件**（`build_task_file` 扩展，一个文件组链 StartUp→Copilot），`maa copilot` 子命令只在 Spike 阶段做快速验证用。

---

## 六、确认交互协议（决策 3：每一步都确认，绝不自动重跑）

复用现有 pending 状态机模式（`pending_confirm` / `pending_selection`），新增 copilot 会话状态：

**① 事前确认（一批一次）**

```
bot：📋 HS-9 计划用作业 38271「xx低配三星」（hot 4.2k / 浏览 1.8w）
     编队：山 精2/40 ✅ · 泥岩 精2/60 ✅ · 银灰 精2/50 ⚠️(doc 提到最好专2，本地无技能数据)
     其余候选：② 39001「高配速通」 ③ 38820「纯地面队」
     回「1」开打；回「2/3」换候选；回「取消」放弃。
```

多关批量（"打全部新关"）：事前把**整批的关卡×作业清单**一次列出确认，开打后逐关不再打扰——只有失败才打断（下条）。

**② 失败后确认（核心变更：报告 → 用户选 → 才动）**

MAA 停下（战斗失败/非三星/编队失败/理智不足）后 worker 正常收尾、释放锁，**不自动重跑**：

```
bot：❌ HS-7 用作业 38271 战斗失败（打到 2:31 暴毙），本关已耗理智 15。
     后面还剩 HS-8、HS-9 没打。怎么办：
     ① 换作业 39001「高配速通」重打 HS-7
     ② 换作业 38820「纯地面队」重打 HS-7
     跳过 —— 不打 HS-7，继续 HS-8
     取消 —— 收工，出总结
```

- 用户回复（编号/跳过/取消）作为新 plan 携带上下文（剩余关卡列表）重新进单飞锁执行。
- 等待决策期间服务空闲，可正常接其他指令。
- TTL：失败确认用户可能不在场，用独立的 `copilot.confirm_ttl_s`（建议 1800s，比 `selection_ttl_s` 长）；过期作废并发一条"已作废，随时再叫我"。
- 理智不足属于"没法继续"而非"作业不行"→ 单独话术（"理智不够了，回「取消」收工或明天再来"），不给备胎清单。

---

## 七、模块落点（全部顺着现有骨架）

| 模块 | 改动 |
|---|---|
| `schemas/task_plan.schema.json` + `prompts/router.system.md` | 新增 copilot 动作：`{stage: "HS-9" 或 "", scope: "single/all_new"}`；few-shot 覆盖「抄作业打 X」「打新活动」 |
| 新 `maa_remote/roster.py` | OperBox 结果解析 + （S3 通过后）skland client + `roster.json` 缓存 + 「更新练度」指令 |
| 新 `maa_remote/copilot_catalog.py` | prts 查询 → content 解析 → 与 roster 硬过滤/软打分 → 候选清单；作业 JSON 落盘 |
| `executor.py` | `build_task_file` 支持 Copilot 任务类型（StartUp→[导航]→Copilot 组链） |
| `router.py` | 新增 copilot 事前确认 + 失败决策两个 pending 状态（§六） |
| `reporter.py` | 解析逐关结果（三星/失败/停在哪）；失败时产出 §六② 的决策消息而非直接总结 |
| `config.toml` | `[copilot]`：`candidates_limit` / `confirm_ttl_s` / `formation_index` / `jobs_dir`；`[skland]`（token 走 env 名引用，延续 key 纪律） |

---

## 八、Spike 清单（回家第一批事，按序）

> ⚠️ 2026-07-08 更新：进度见每项前缀与 **§十一**。执行时改用 `scripts/spike_copilot.py`（不是手打命令）。

- [x] **S1（API 部分）✅**：`/copilot/query` 关卡过滤参数 = `level_keyword`，值须是内部 `level_id`（非显示号）；content/`/arknights/level` 形状已核实。详见 §十一。作业 stdout 样本仍待抓（并入 S4）。
- [x] **P0 / S4b（生产执行路径）✅**：`maa run <tasks.json>` 接受 `{type:Copilot}` 并在 StartUp 后组链执行（`spike_copilot.py copilot-run`）。这是真正要上线的路径，比 S1 原写的 `maa copilot` 子命令更关键。
- [~] **S2 主界面导航**：已定案 —— `copilot_list` **不做"主页→地图"**（实测 540 次盲滑、0 点击、卡主页；见 §十一）→ **必须自建"主页→终端→地图"导航 override（§五方案 2）**。
- [ ] **S3 森空岛 API 可用性**：照 arknights-mower 现行实现走 token→cred→player/info，确认返回里有 `chars[].skills[].specializeLevel` 和模组。跑通 → 主数据源；跑不通 → OperBox-only，不再恋战。
- [ ] **S4 OperBox 经 maa-cli**：`spike_copilot.py operbox`；识别结果从哪拿（stdout / `asst.log` 的 all_oper/own_opers 回调）；抓样本定解析规则。
- [x] **S5 `--batch` 免交互 ✅**：`maa run` 的 Copilot 路径全程无交互阻塞（交互式确认只在 `maa copilot` 子命令，我们不走）。

**核心可行性（P0/S1-API/S5）✅ 已成立**；headless catalog 原型已在 `spikes/copilot_catalog.py` 跑通并留存报告；S2 已确认需要导航开发；S3/S4 决定练度数据的精度上限。

---

## 九、风险矩阵

| 风险 | 缓解 |
|---|---|
| 森空岛不可用（仓库已 archive） | S3 早定案；OperBox-only 保"能开起来"，专精盲区靠确认流程兜底 |
| 主界面导航不通 | OCR override（一次投入）；再不行本功能降级为"需游戏已停在活动内"并明确告知 |
| 作业质量参差 / 中途翻车 | 官方"非三星即停" + 失败必确认，单次损失上限一关理智 |
| 新活动首日：资源未更 / 作业还少 | hot-update 前置；查询为空/过少时回"该关还没有靠谱作业，晚点再试" |
| prts API 变动 | 与 maa-cli 同源，风险低；参数名疑点已进 S1 |
| 借助战不可用 | 设计上不依赖：硬过滤要求全自有（groups 缓冲） |

---

## 十、非目标（本期不做）

- 借助战编队（MAA 明确不借）、肉鸽、保全派驻、悖论模拟（官方要求关自动编队/手动准备）
- 危机合约（词缀/等级选择复杂，作业匹配语义不同）
- 无人值守全自动连打不确认（与决策 3 冲突）
- prts.plus 作业集（/set）、上传/评分回写

---

## 十一、Spike 执行记录（2026-07-08，headless）

> 环境：本机 headless coding session（无游戏/模拟器/skland token）。只跑了 S1 里**不依赖游戏**的 prts API 部分；S2/S3/S4/S5 仍需在游戏机上跑（见下方 `scripts/spike_copilot.py`）。

### ✅ S1-API 已定案：`/copilot/query` 关卡过滤

- **参数名就是 `level_keyword`（snake_case）**，但**值必须是关卡内部 `level_id`（或 `stage_id`），不是显示关卡号**。这解释了草案里 `levelKeyword=1-7` "疑似未生效"：`1-7` 不是内部 id，主线 1-7 的内部名是 `main_01-07`。实测（`order_by=hot`）：
  - `level_keyword=1-7` → total=38486（**未过滤**，等于全量）
  - `level_keyword=main_01-07` → total=295 ✅
  - `level_keyword=activities/act22side/level_act22side_ex02`（level_id）→ total=11 ✅
  - `level_keyword=act22side_ex02`（stage_id）→ total=11 ✅（keyword 是子串匹配）
  - `level_keyword=FC-EX-2`（cat_three 显示号）→ **未过滤** ❌
- **结论**：§四 step1「显示号 → level_id」映射是**硬前置**，不是可选校验。`/arknights/level` 提供 `cat_three(显示号) ↔ level_id` 映射，正是所需。
- **order_by=hot 生效**（top 结果是当期热门危机合约作业）。

### ⚠️ content schema 三处修正（vs §2.6）

1. **`content.stage_name` 可能为空**（实测 `act22side_ex02` 热门作业该字段是 `""`）→ **不能用它来"校验没串关"**；串关校验只能靠 query 的 `level_keyword` 过滤本身 + `/arknights/level` 映射。
2. **`doc` 是对象 `{title, details}`，不是自由文本字符串**。§三 缓解措施2 的 DeepSeek 解析输入要拼 `doc.title + doc.details`。
3. **query 结果项自带更好的质量信号**：除 `hot_score/views/like/dislike` 外，还有 `rating_level` / `rating_ratio` / `rating_type` / `not_enough_rating` / `available`。§四 step4 软打分应优先用 `rating_level`（社区评级）而非裸 hot。

### ✅ `/arknights/level` 形状确认

- 扁平 list（本次 3247 项），字段 `level_id / stage_id / cat_one("活动关卡") / cat_two(活动名) / cat_three(显示号如 FC-EX-2) / name / width / height`。§2.5 描述正确（控制台里的乱码只是 GBK 渲染，数据本身 UTF-8 正常）。

### ✅ S4b + S5 已在游戏机实测（2026-07-08，`spike_copilot.py copilot-run 53355`，1-7）

用 `maa run <tasks.json>`（tasks=[StartUp, Copilot{filename=本地作业, formation=true, use_sanity_potion=false}]）实跑，asst.log 逐帧为证：

- **P0（S4b）✅ PASS**：`maa run` **接受 `Copilot` 任务类型并在 StartUp 之后自动组链执行**。`[StartUp] Completed (1m27s)` → `[Copilot]` 作为 taskid=2、taskchain="Copilot" 起跑。生产执行模型（§五：`build_task_file` 出 Copilot 任务、`maa run` 串起来）**机制成立**，不需要退回 `maa copilot` 子命令。
- **S5 ✅ PASS（顺带）**：`--batch` 下 Copilot 全流程无交互阻塞（含自动编队），**没有**弹出"set up your formation manually"。那个交互式确认只在 maa-cli 的 `copilot` **子命令**代码里，生产走的 `maa run` 路径无此问题——S5 顾虑对生产路径不成立。
- **自动编队 ✅**：`BattleFormationTask ... Completed (28.6s)` —— 本账号能满足作业 53355 的 groups，MAA 自动编队在到达编队屏时工作正常。
- **⚠️ S2 被实测证实为真实且不可绕过的缺口**：Copilot 单 `filename` 模式的 taskchain 是 `BattleStartPre → BattleFormationTask → BattleStartAll`，**全程没有任何"关卡导航"子任务**——它直接假设"你已在编队选择界面"。而 StartUp 收尾停在**主界面（终端）**，于是 `BattleStartPre` 先报错、`BattleStartAll`（找"开始行动"按钮）重试 3 次失败 → `TaskChainError Copilot`。**非分辨率问题**（编队 OCR/模板匹配正常）。结论：**"主界面→关卡"导航必须由我们补**，Copilot 参数本身不 bridge。§五方案（`copilot_list` 战斗列表模式 or 自定义导航）里，注意：`copilot_list` 也只从**地图界面**起步自动切关，"主界面→地图"这一段仍是 S2，跑不掉。

### ❗ S2 首测（`copilot_list`，2026-07-08，1-7）——已定案：copilot_list 不做"主页→地图"导航

`spike_copilot.py copilot-list 53355 main_01-07`，中途人工停止。**用户全程盯模拟器：游戏从没离开主页(首页)，没点进终端。** asst.log 逐行核实（run 起点 line 61146 `MultiCopilotTaskPlugin`）：

- **全程只有 `"action":"Swipe"`（540 次），0 次点击/tap**。`FullStageNavigation` / `ChapterSwipeToTheRightAndPlot` / `StageNavigationSlowlySwipeLeft` 三者**全是 `algorithm:JustReturn` 的无条件盲滑**（不是"识别到再滑"）。
- `MultiCopilotTaskPlugin::find_stage` 在 OCR 找关卡码，读到的却是**主页顶栏垃圾**（日期 `2026/07/08 00:52`、理智数、龙门币数）→ 铁证它就卡在主页上盲滑。
- **⚠️ 修正 2026-07-08 早先的错误结论**：不是"章节导航滑过头"、也不是"选错主线关"（当时把 `FullStageNavigation` 任务**名**误读成"真在导航"）。真相：**copilot_list（`MultiCopilotTaskPlugin`）根本不含"主页→点终端→进地图"这步，它假设你已在关卡地图界面**，只做 OCR-找关卡 + 盲滑。StartUp 收尾停主页 → 无锚点 → 永远盲滑。**与主线/活动无关**，是"主页→地图"硬缺口。这反而印证了 §2.1 原文"在关卡所在的地图界面开始"。

**S2 定案**：copilot_list 只负责"地图内找到并进关"，**不负责"主页→地图"**。→ **必须自建"主页→终端→(活动/主线)地图"导航 override（§五方案2）**，到地图后再交给 copilot_list。可参考：MAA 普通 `Fight` 任务本就能"主页→关卡"导航（能力在 MAA 里存在，只是没接进 copilot 插件）——先查能否复用其导航子任务，再决定自写多少。

### ⛔ 仍需游戏机上跑的 spike

- **S2（头号阻塞，已定案要自建导航）**：copilot_list 不做"主页→地图"（见 §十一）。开发"主页→终端→活动地图"override（§五方案2，先查能否复用 MAA `Fight` 的导航子任务）；待 live 活动关时手点到活动地图，验证 copilot_list 能否 find_stage+进关开打（确认 override 只需负责到地图为止）。
- S3（森空岛可用性）、S4（OperBox 经 maa-cli 拿识别结果，`spike_copilot.py operbox`）。
