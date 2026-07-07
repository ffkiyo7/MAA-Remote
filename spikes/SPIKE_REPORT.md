# Spike Report: copilot 自动战斗 — headless API 验证 & 匹配原型

> **日期**: 2026-07-07  
> **环境**: headless Linux (无模拟器/游戏/Skland token)  
> **范围**: 仅 prts.plus API 端 — 不涉及游戏端 S2/S3/S4/S5  
> **对应设计文档**: `docs/superpowers/specs/2026-07-07-copilot-auto-battle-design.md`  
> **Review**: Claude Code (Sonnet 5) reviewed 2026-07-07, 4 issues fixed (see §八)

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

### 测试结果

| 关卡 | 作业总数 | 通过 (mock Box) | 不通过 |
|---|---|---|---|
| FC-EX-2 (活动EX) | 11 | 3 | 8 |
| 1-7 (主线) | 10 | 3 | 7 |
| LS-5 (资源本) | 10 | 2 | 8 |

### 硬过滤逻辑

- 逐个检查 `opers[]` 干员是否在 roster 中
- 缺失时查 `groups[]`: 分组内**任意一人**拥有 → 该槽位通过
- 检查 `requirements.elite/level/skill_level`
- `skill_level` 标注 "⚠️ 盲区" (OperBox 无此数据)

### 软打分 (0-100)

- `rating_level` × 4 (max 40)
- `hot_score` log 归一化 (max 20)
- `views` log 归一化 (max 15)
- like ratio (max 15)
- 上传新鲜度 (max 10)

### Doc 信号解析

关键词扫描 `doc.title + doc.details`:
- 🟢 低配 / 低练 / 无专精 / 精一 / 平民
- 🔴 满专 / M3 / 高配 / 速通 / 专三

### Mock Roster

用一份中等偏上的 6★ 精二 Box 做硬过滤验证 (山/银灰/小羊/能天使/塞雷娅等 15 人)。

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
