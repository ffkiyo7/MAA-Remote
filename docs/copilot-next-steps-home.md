# 抄作业功能：回家那台机怎么继续

> 给「家里那台真跑游戏的机器」看的大白话说明。开发机（aimall）已经把**离线核心**写完并测过，
> 剩下的全是**要碰真游戏 / 真练度 / 真 log** 的活。本文告诉你按什么顺序推进、每步敲什么命令、
> 哪些要把结果发回来让 Claude 接着写。
>
> 配套设计文档：[docs/superpowers/specs/2026-07-07-copilot-auto-battle-design.md](superpowers/specs/2026-07-07-copilot-auto-battle-design.md)

---

## 一、现在到哪一步了

「抄作业」的**大脑**已经建好并且全部通过单元测试（233 passed）：

- 从 prts.plus 拉作业 → 按你的练度硬过滤 + 软打分排序 → 候选清单
- 飞书里的**事前确认**（列首选 + 备选，回「1」开打 / 回编号换 / 回「取消」）
- 选中后把作业**落盘到本地** + 拼出 `StartUp → [导航] → Copilot` 的任务链
- **失败后决策**状态机（换作业 / 跳过 / 取消）
- 进度推送里 Copilot 有了中文标签

但这套东西**一次真游戏都没碰过**。所有跟游戏、跟你账号练度、跟真实战斗日志有关的部分都还没接。

---

## 二、一眼看清：现在能用 / 还不能用

| 能力 | 现状 |
|---|---|
| 飞书说「抄作业打 X-Y」被识别成抄作业意图 | ✅ 已实现（建议跑一次 live eval 确认，见任务 6） |
| 拉作业、按练度过滤、出候选清单、事前确认 | ✅ 逻辑就绪，但**需要先建关卡索引**（任务 1），练度不准（任务 2 前用降级模式） |
| 选中作业→落盘→拼任务链 | ✅ 已实现 |
| **真的打进关卡** | ❌ **卡在这**：主页→活动地图导航没做（S2，任务 4），到不了关卡 |
| 逐关汇报三星/失败、失败后给备胎 | ❌ 状态机在，但没接真实战斗日志（#6，任务 3），worker 还没调用它 |
| 按你账号真实练度精准过滤 | ❌ 要 OperBox 样本（#10，任务 2）；没有前用「空练度降级」（全部标⚠️不淘汰） |

---

## 三、先把代码弄到这台机

开发机已经把这批推到 GitHub 的 `feat/copilot-catalog-foundation` 分支：

```bash
cd /path/to/MAA-remote          # 家里那台的仓库目录（E:\code\MAA-remote）
git fetch origin
git checkout feat/copilot-catalog-foundation
git pull
.venv\Scripts\pip install -r requirements.txt   # 依赖没变，跑一下保险
.venv\Scripts\python -m pytest -q               # 应 233 passed
```

> 你的 `config.toml` 是本机专属、不进 git，不会被覆盖。`[copilot]`/`[skland]` 两段整段可缺省，
> 不加也能用默认值（见 `config.example.toml` 里的注释）。

---

## 四、按顺序推进（每步要敲什么 / 要发回什么）

### 任务 1 · 建关卡索引 `stage_catalog.json`（必须，先做）

抄作业要把「HS-9」这种显示号翻译成内部 `level_id` 才能查作业，靠的就是这张表。**纯联网，不用开游戏**（在这台或开发机都行）。

```bash
.venv\Scripts\python spikes/build_stage_catalog.py
# 产出 spikes/fixtures/stage_catalog.json
```

然后让抄作业能找到它，二选一：

- 把文件复制到 `<config_dir>/copilot/stage_catalog.json`（默认位置），或
- 在 `config.toml` 里加 `[copilot]` 段设 `stage_catalog_json = "你的绝对路径"`。

做完这步，抄作业就能出候选清单了（练度先按降级模式，见任务 2）。

### 任务 2 · OperBox 练度样本（#10，让过滤按你的号来）

没有它，抄作业用「空练度」跑——所有作业都标⚠️、不淘汰缺人的作业，不精准。要精准就得拿到你账号的干员识别结果：

```bash
.venv\Scripts\python scripts/spike_copilot.py operbox
```

- 跑完把 **stdout** 和 **asst.log 里 `all_oper` / `own_opers` 那几段**存下来。
- **发回给 Claude** → 我据此写 `roster.py` 的真实解析，把它变成 `roster.json` 缓存（放 `<config_dir>/copilot/roster.json`，抄作业会自动读）。

### 任务 3 · 抄作业实战 log（#6，接失败决策 + 逐关汇报）

先做完任务 1，手上有一份能用的作业 id。然后各跑一次成功和失败的，**分开存 log**：

```bash
# 成功的一关（挑你能打过的关 + 靠谱作业）
.venv\Scripts\python scripts/spike_copilot.py copilot-run <作业id> <显示号如 HS-9>
# → 把这次的 asst.log 存成 copilot_ok.log

# 必翻的一关（挑个你打不过的关/作业，故意让它失败）
.venv\Scripts\python scripts/spike_copilot.py copilot-run <作业id> <显示号>
# → 把这次的 asst.log 存成 copilot_fail.log
```

- **两份 log 发回给 Claude**（一定要分开命名，别混一起——设计文档里 S4b 那次就是成功/失败混写导致自相矛盾）。
- 我据此写 `reporter.py` 的逐关结果解析，并把 worker 接到 `start_failure_decision`——这样失败后飞书才会真弹「换作业/跳过/取消」。

> 注意：任务 3 能真正打起来，**依赖任务 4（导航）先通**，否则会卡在主页。如果任务 4 还没好，可以手动把游戏点到关卡的编队界面再跑 `copilot-run`，先把「到了关卡之后」这段的 log 抓出来。

### 任务 4 · 主页→活动地图导航（S2，头号阻塞）

实测过：Copilot / copilot_list 都**不管「主页→地图」这一段**，会卡在主页盲滑（设计文档 §十一）。必须自己补一段导航。

- Claude 先出一份导航 override 的**任务 JSON 草稿**（参考 MAA 普通 `Fight` 本来就会的「主页→关卡」）。
- 你在这台机的模拟器上试：能不能真的点进当期活动地图。
- 不同活动 UI 不一样、OCR 点位稳不稳，只有你盯着模拟器才能验。

这步通了，抄作业才算真能端到端打关。

### 任务 5 · 森空岛练度（#9，可选增强）

补 OperBox 的盲区（技能专精/模组）。**纯 HTTP，要 token，不用开游戏**——有 token 在开发机就能测。有的话把 token 发给 Claude（或设成环境变量），我把 skland client 写完并当场验能不能拿到数据。跑不通就一直用 OperBox，不影响主线。

### 任务 6 · 意图识别 live 回归（确认 LLM 真会判成抄作业）

改过 prompt/schema，按约定要跑一次真 LLM 回归（**要 `DEEPSEEK_API_KEY`，不用开游戏**）：

```bash
.venv\Scripts\python -m maa_remote.eval_router
```

evals 里已经加了 3 条抄作业用例，确认 DeepSeek 把「抄作业打 HS-9」「打新活动」稳定判成 `action=copilot`。

### 任务 7 · DeepSeek 分析作业 doc（可选增强，单独 PR）

现在软打分用的是关键词启发式。接 DeepSeek 读作业的 `doc.title + doc.details`（「低配/满专/需专精」）能更准。这是独立一块，不阻塞任何东西，Claude 后面单独做。

---

## 五、想现在就先试试（最小可用路径）

即使 #6/#10/S2 都没做，做完**任务 1** 就能先体验到「大脑」：

1. 建好 `stage_catalog.json`（任务 1）。
2. 飞书 DM 发「抄作业打 <当期某关>」。
3. 你会收到**候选清单 + 事前确认**（练度用降级模式，作业都标⚠️）。
4. 回「1」→ 它会把作业落盘、拼出 `StartUp→Copilot` 任务链、真的调 maa。
5. **但因为 S2 没做**，实际大概率会卡在主页 / 编队屏报错——除非你手动把游戏点到关卡的编队界面。

也就是：**大脑通了，手脚（导航）还没接**。把任务 1~4 做完，才是完整可用。

---

## 六、一句话总结优先级

**任务 1（建索引，必须）→ 任务 4（导航，头号阻塞）→ 任务 3（实战 log 接失败/汇报）→ 任务 2（练度精准化）**；
任务 5/6/7 可选、随时插。凡是「发回给 Claude」的（任务 2 的 OperBox 样本、任务 3 的两份 log、任务 5 的 token），拿到我就能继续写对应的代码。
