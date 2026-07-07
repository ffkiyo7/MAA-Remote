# Spike Report: copilot 自动战斗 — headless API 验证 & 匹配原型

> **日期**: 2026-07-07  
> **环境**: headless Linux (无模拟器/游戏/Skland token)  
> **范围**: 仅 prts.plus API 端 — 不涉及游戏端 S2/S3/S4/S5  
> **对应设计文档**: `docs/superpowers/specs/2026-07-07-copilot-auto-battle-design.md`  
> **Review**: Claude Code (Sonnet 5) 首轮 6 项已修 (§八); Opus 4.8 xhigh 复审又发现 2 个 P0 + 3 个 P1/P2, 均已修复 (§九)

---

## 一、能跑哪些 Spike

| Spike | 依赖游戏 | Headless 可跑? | 结果 |
|---|---|---|---|
| **S1-API** (prts.plus 查询) | ❌ | ✅ | 扩展验证通过 |
| **stage_catalog** (关卡映射) | ❌ | ✅ | 3247 关卡映射表已构建 |
| **copilot_catalog** (匹配原型) | ❌ | ✅ | 完整管线已跑通 |
| S2 (主界面导航) | ✅ | ❌ | 需游戏机上的模拟器 |
| S3 (森空岛 API) | ✅ (需 token) | ❌ | 需有效 Skland token |
| S4 (OperBox via maa-cli) | ✅ | ❌ | 需模拟器+游戏 |
| S5 (batch 免交互) | ✅ | ❌ | 7/8 已在游戏机验证 |

## 二、S1-API 扩展验证

### 2.1 关卡过滤 `level_keyword` 确认

| 参数值 | total | 生效? |
|---|---|---|
| (无) | 38,487 | 全量 |
| `FC-EX-2` (显示号) | 38,487 | ❌ 未过滤 |
| `act22side_ex02` (stage_id) | 11 | ✅ 子串匹配 |
| `activities/act22side/...` (level_id) | 11 | ✅ 精确匹配 |
| `main_01-07` (stage_id) | 295 | ✅ 子串匹配 |
| `obt/main/level_main_01-07` (level_id) | 295 | ✅ 精确匹配 |

**结论**: 与设计文档 §十一 完全一致。显示号必须先映射到 level_id/stage_id。

### 2.2 Content Schema 批量统计 (100 份热门作业)

| 字段 | 统计 |
|---|---|
| `stage_name` 为空 | 0% (top 100) — ⚠️ 但特定关卡(FC-EX-2)有大量空值 |
| `doc` 类型 | 100% dict (title+details) — 非自由文本 |
| 有 `requirements` | 60% |
| 有 `groups` (可替换干员) | 63% |
| 干员数分布 | 中位数 3-4, 范围 0-12 |
| `rating_level` 分布 | 55% 满星(10★), 40% 8-9★ |

### 2.3 API 响应包裹

- `/arknights/level` → `{status_code, data: [...]}`
- `/copilot/query` → `{status_code, data: {total, has_next, page, data: [...]}}`
- `content` 在 query 结果中已含全文 JSON — 无需逐条 `/copilot/get`

### 2.4 Groups 结构确认

```json
{"name": "工具人", "opers": [
  {"name": "CONFESS-47"},
  {"name": "孑", "requirements": {"elite": 0, "level": 1}}
]}
```

Groups 内 `opers` 是 **dict 列表** (`{name, requirements?}`)，不是字符串数组。

### 2.5 质量信号字段 (query 结果自带)

除 `hot_score/views/like/dislike` 外还有:
- `rating_level` (0-10 社区评级)
- `rating_ratio`
- `rating_type`
- `not_enough_rating`
- `available`

建议 §四 step4 软打分优先用 `rating_level` 而非裸 hot_score。

## 三、Stage Catalog 映射表

产出 `spikes/fixtures/stage_catalog.json` (459KB, 3247 关卡):

```json
{
  "display_to_level": {"FC-EX-2": "activities/act22side/level_act22side_ex02", ...},
  "stage_to_level": {"act22side_ex02": "activities/act22side/level_act22side_ex02", ...},
  "activity_stages": {"FC-EX-2": "activities/act22side/level_act22side_ex02", ...}
}
```

去重策略: 优先非 `#f#` (迷雾模式) 变体。

## 四、Copilot Catalog 原型

`spikes/copilot_catalog.py` — 完整匹配管线:

```
显示号 → level_id 映射 → /copilot/query → content 解析 → 硬过滤 → 软打分 → 候选清单
```

### 测试结果 (默认 limit=10, OperBox 口径 mock Box)

| 关卡 | 拉取 | 通过·净 | 通过·带风险 | 未通过 |
|---|---|---|---|---|
| FC-EX-2 (活动EX) | 10 | 1 | 0 | 9 |
| 1-7 (主线) | 10 | 2 | 0 | 8 |
| LS-5 (资源本) | 10 | 0 | 0 | 10 |

> 复审前 groups 从不校验导致 FC-EX-2 有 3 份缺干员作业假通过 (52492/45316 等)、
> 1-7 的 95104、LS-5 的 64432 (纯 group 槽) 均假通过；修复后这些全部正确淘汰。
> LS-5 全灭是因 mock 缺低星核心 (讯使/翎羽/黑角/娜仁图亚), 属预期。

### 硬过滤逻辑 (§2.4: opers 与 groups 成员不相交)

- **opers[]**: 每一名都必须自有且 `elite/level` 达标 — 缺人或精英/等级不足 → **淘汰**
- **groups[]**: 每个 group 是**独立槽位**, 需至少一名自有且达标成员满足 → 缺则淘汰
  (不是 opers 的替补; 二者不相交)
- 无显式 `requirements.elite` 时才从 `skill` 序号隐含推断 (技能2→精一, 技能3→精二);
  显式要求优先
- `skill_level` 是 OperBox 盲区 → 仅产出 **⚠️ 风险标注, 不淘汰** (§三)
- 皮肤别名归一 (凛御银灰→银灰, 纯烬艾雅法拉→艾雅法拉): 后缀匹配 + `SKIN_ALIASES` 特例表

### 软打分 (0-100)

- `rating_level` × 4.5 (max 45) — 社区评级最可靠
- `hot_score` log 归一化 (max 18)
- `views` log 归一化 (max 12)
- like ratio (max 15)
- 上传新鲜度 (max 10)
- doc 信号: 低配友好 +2/个 (max 6), 高配风险 −3/个 (max 9)
- 练度裕度 (max 5)
- 技能盲区等风险 −2/项

### Doc 信号解析

关键词扫描 `doc.title + doc.details` (仅供软打分微调, 收紧了易误判词):
- 🟢 低配 / 低练 / 无专精 / 三级技能 / 平民 / 养老
- 🔴 满专 / M3 / 高配 / 专三 / 满潜 / 高练度

### Mock Roster

用一份 OperBox 口径的 6★ 精二 Box (15 人, 山/银灰/艾雅法拉/能天使/塞雷娅等) 做硬过滤验证。
**刻意不含 `skill_level`** — 与真实 OperBox 回调一致 (§2.4), 用于暴露技能盲区风险路径。

## 五、可提 PR 的产物

```
spikes/
├── build_stage_catalog.py              # 关卡映射表构建器
├── copilot_catalog.py                  # 匹配管线原型
├── fixtures/
│   ├── stage_catalog.json              # 完整关卡映射 (459KB)
│   ├── activity_stage_map.json          # 活动关卡速查表 (71KB)
│   ├── copilot_raw_FC-EX-2.json        # 原始 API 响应
│   ├── copilot_candidates_FC-EX-2.json # 候选清单
│   ├── copilot_raw_1-7.json
│   ├── copilot_candidates_1-7.json
│   ├── copilot_raw_LS-5.json
│   └── copilot_candidates_LS-5.json
└── SPIKE_REPORT.md                     # 本文件
```

## 六、发现的问题与建议

1. **`stage_name` 为空的作业**: FC-EX-2 的热门作业 `stage_name=""` (11 份中至少 1 份) → 不能依赖 `stage_name` 校验串关，必须靠 `level_keyword` 查询参数本身
2. **Groups 中 opers 是 dict**: 已有代码假设 `groups[].opers[]` 是字符串的会被打脸
3. **Rating 信号比 hot 更稳**: 热门全在 8-10★, 冷门作业 rating_level=0 → 加上 rating 阈值过滤低质作业
4. **分页无重叠**: page1/2/3 的 ID 互不重复 ✅

## 七、仍需游戏机上跑

- S2 (主界面→地图导航 override)
- S3 (森空岛 API 可用性)
- S4 (OperBox 识别结果解析)
- 真实 roster 数据驱动完整端到端测试

## 八、Claude Code Review 修复记录 (2026-07-07)

Claude Code (Sonnet 5) 审查了 `build_stage_catalog.py` + `copilot_catalog.py` + `SPIKE_REPORT.md`，发现并修复了以下问题：

1. **🔴 `--roster` 参数被忽略** — `hard_filter()` 永远用 `mock_roster`，不读取真实 roster。已修复：`--roster` 指定的文件现在正确传入 `hard_filter()`，mock 仅作为默认 fallback。

2. **🟡 skill→elite 隐含推断缺失** — 设计文档 §三 要求从 `opers[].skill` 推断精英需求（技能2→精一，技能3→精二）。已添加 `_infer_elite_from_skill()`，在 `requirements` 为空时补上隐含精英检查。

3. **🟡 Groups 替补不检查练度** — 原实现只检查替补干员"是否拥有"，不检查练度是否达标。已修复：group 内每个替补干员都经过完整的 `_check_op_requirements()` 校验（显式 requirements + skill→elite 推断）。

4. **🟡 `doc.title` 缺失** — 候选清单未包含作业标题，后续接 §六 确认交互协议时会缺字段。已修复：`candidates[].title` 现已包含 `doc.title`，终端输出也显示「标题」。

5. **🟡 Catalog 逻辑漂移** — `build_stage_catalog.py` 和 `copilot_catalog.py` 各有独立的下载+构建逻辑。已修复：`copilot_catalog.py` 改为直接读取 `build_stage_catalog.py` 产出的 `stage_catalog.json`，文件缺失时报错提示先运行构建脚本。

6. **🟡 去重碰撞无记录** — `build_stage_catalog.py` 对同名显示号（如 TN-1 跨 6 个活动）任选第一个，未记录。已添加碰撞检测日志（发现 811 个冲突，多为同 level_id 重复，少量如 TN-1/VS-1 是真实冲突需后续处理）。

## 九、Opus 4.8 xhigh 复审修复记录 (2026-07-07)

Opus 4.8 (xhigh) 用仓库现成 fixtures 实跑复现, 在硬过滤里发现两个"两头都错"的 P0 (既假通过又误击落), 以及 3 个 P1/P2。全部已修 (`copilot_catalog.py` + `build_stage_catalog.py`)：

1. **🔴 P0 · groups 槽位从不被校验 → 缺干员作业假通过** — 真实 schema 里 `opers` 与 `groups` 成员**完全不相交** (0/106、0/41、0/266)，但旧 `hard_filter` 只在"某 oper 缺失且其名恰好出现在某 group"时才查 group，该条件在真实数据里**永不成立** → group 从头到尾没被验证。FC-EX-2 的 52492/45316、1-7 的 95104、LS-5 的 64432 (纯 group 槽) 全部假通过。**已修复**：groups 改为**独立成槽**——每个 group 需至少一名自有且 elite/level 达标的成员；opers 照旧全员校验。上述作业现全部正确淘汰。

2. **🔴 P0 · `skill_level` 被当硬淘汰, 真实 OperBox roster 必然全灭** — §三 明确 skill_level 是"盲区"、应**标注**而非淘汰；但旧代码 `return` 失败字符串 → `pass=False`。而真实 OperBox 回调不含 skill_level (§2.4) → 任何带 skill_level 要求的作业每个干员都判失败 → 整份误杀 (占比约三成)。旧 mock 给了 `skill_level:7` 这个真实数据源没有的字段, 掩盖了必炸路径。**已修复**：skill_level 不达标/无数据只产出 **⚠️ 风险标注, 不参与 `pass`**；mock roster 同步改为**不含 skill_level**, 与 OperBox 口径一致, 暴露该路径。

3. **🟡 P1 · 缺"通过但有风险"档 + soft_score 未落地 §四 step4** — `analyze_doc` 的 `signals` 与 requirements 裕度都没进 `soft_score`；排序只有二元 `(pass, score)`。**已修复**：candidate 拆出独立 `risks` 通道 (硬失败与风险不再混在 `issues`)，新增 `risky` 档；排序改为 `(pass, 无风险, score)`——净通过 ✅ > 带风险 ⚠️ > 未通过 ❌；`soft_score` 纳入 doc 信号 (±) 与练度裕度 (+5) 与风险扣分 (−2/项)。

4. **🟡 P1 · 皮肤别名导致"拥有却判缺"** — fixtures 里 opers 带皮肤前缀 (凛御银灰/纯烬艾雅法拉/寒芒克洛丝/历阵锐枪芬)，精确名匹配对规范名 Box 返回 `None`。**已修复**：`Roster.get()` 归一皮肤名——最长自有后缀匹配 (≥2 字) + `SKIN_ALIASES` 特例表 (覆盖单字规范名如 历阵锐枪芬→芬)。

5. **🟡 P1 · 活动显示号碰撞无当期消歧** — 复用号 (TN-1 等) 静默映射到某一 level_id, 可能查错关。**已修复**：`build_stage_catalog.py` 只把**指向多个不同 level_id** 的显示号算作碰撞并写入 `stage_catalog.json` 的 `display_collisions`；`copilot_catalog.py` 命中碰撞时**告警**并列出候选, 支持 `--level-id` 指定当期消歧。

6. **🟢 P2 · skill→elite 推断覆盖显式要求** — 旧代码在显式 `requirements` 之上无条件叠加隐含推断。**已修复**：只在**无显式 `elite`** 时才隐含推断, 显式要求优先。

7. **🟢 P2 · 健壮性** — 输出 `hot_score` 用裸值 `:.1f`, API 返回 `null` 时 `TypeError`；`fetch_copilots` 无 HTTP 错误处理。**已修复**：candidate 数值字段统一 `or 0` 兜底；`fetch_copilots` 捕获 `HTTPError/URLError/JSONDecodeError` 并对 `data=None` 安全兜底。

8. **🟢 P2 · analyze_doc 启发式 + 文档漂移** — 去掉易误判关键词 (精一/速通)；`SPIKE_REPORT §四` 的软打分权重 (×4/max40…) 与代码 (×4.5/max45…) 对不上。**已修复**：收紧关键词表；§四 权重、硬过滤逻辑、Doc 信号、Mock Roster 均已同步为代码实际值。

> **回归**: `python3 spikes/copilot_catalog.py FC-EX-2 --limit 5` 及 1-7 / LS-5 (limit 10) 全部跑通；
> 三份 P0 复现作业 (52492/45316、95104、64432) 均已正确淘汰, skill_level 作业改走 ⚠️ 风险通道。
