"""抄作业匹配管线（设计文档 §四）：prts 查询 → content 解析 → 硬过滤 → 软打分 → 候选清单。

硬过滤/软打分逻辑逐字沿用 spike（`spikes/copilot_catalog.py`，经 Sonnet+Opus 两轮 review）。
产品化改动：
  - `fetcher` 可注入 —— 默认走真实 HTTP；离线单测传假 fetcher 喂 fixtures。
  - `analyzer` 可插拔（seam）—— 默认启发式 `analyze_doc`；后续接 DeepSeek 只换这一个参数。
  - `now` 可注入 —— 新鲜度打分确定化，单测不依赖真实时间。
  - 关卡定位不再 `sys.exit`，改抛 `StageResolutionError`；碰撞信息随结果返回给上层消歧。
作业落盘 / plan 引用（缺口 #A）在本模块之上单独一层，不混进匹配逻辑。
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from maa_remote.roster import Roster

QUERY_API = "https://prts.maa.plus/copilot/query"
UA = "maa-remote"


class StageResolutionError(Exception):
    """显示号无法映射到 level_id。"""


class CopilotFetchError(Exception):
    """prts 查询失败（HTTP/网络/JSON）。"""


# =============================================================================
# 关卡定位（显示号 → level_id，硬前置；碰撞需消歧）
# =============================================================================

def load_stage_catalog(path: str) -> dict:
    """加载 /arknights/level 产出的映射表（build_stage_catalog.py 产出）。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_level_id(
    catalog: dict, stage_display: str, override: Optional[str] = None
) -> tuple[str, list[str]]:
    """显示号 → (level_id, 碰撞候选列表)。

    - override 指定时直接用它（当期消歧）；
    - 未知显示号 → StageResolutionError；
    - 碰撞显示号（跨活动复用，指向多个 level_id）→ 返回默认 level_id + 全部候选，
      由上层决定是否要求 override。
    """
    if override:
        return override, []
    level_id = (catalog.get("display_to_level") or {}).get(stage_display)
    if not level_id:
        raise StageResolutionError(f"未知关卡显示号: {stage_display}")
    collisions = (catalog.get("display_collisions") or {}).get(stage_display, [])
    return level_id, list(collisions)


# =============================================================================
# 查询（可注入 fetcher）
# =============================================================================

def http_fetch_copilots(level_id: str, limit: int = 10) -> list[dict]:
    """默认 fetcher：查 /copilot/query，返回 items（content 已解析为 content_parsed）。"""
    params = f"level_keyword={level_id}&orderBy=hot&page=1&limit={limit}"
    url = f"{QUERY_API}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise CopilotFetchError(f"查询失败 HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise CopilotFetchError(f"网络错误: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise CopilotFetchError("查询响应非合法 JSON") from e
    return _parse_query_rows(resp)


def _parse_query_rows(resp: dict) -> list[dict]:
    """把 /copilot/query 响应体解析成 items，每项补 content_parsed。

    先校验业务成功再解析：prts 成功码是 200（HTTP 风格，非 0）。业务失败或结构异常
    抛 CopilotFetchError，避免上层把"查询失败"误当成"没有可用作业"。status_code 缺省时
    从宽放行（只在明确非 200 时判失败）。
    """
    if not isinstance(resp, dict):
        raise CopilotFetchError(f"查询响应结构异常: 顶层非 dict ({type(resp).__name__})")
    status = resp.get("status_code")
    if status is not None and status != 200:
        raise CopilotFetchError(f"查询业务失败: status_code={status}")
    data = resp.get("data")
    if not isinstance(data, dict):
        raise CopilotFetchError(f"查询响应结构异常: data 非 dict ({type(data).__name__})")
    rows = data.get("data") or []
    items = []
    for item in rows:
        content = item.get("content", "")
        if isinstance(content, str):
            try:
                item["content_parsed"] = json.loads(content)
            except json.JSONDecodeError:
                item["content_parsed"] = {}
        else:
            item["content_parsed"] = content or {}
        items.append(item)
    return items


# =============================================================================
# 硬过滤（§四 step3, §2.4）—— 逐字沿用 spike
# =============================================================================

def _infer_elite_from_skill(skill_index: int) -> int:
    """skill→精英隐含要求：2技能需精一，3技能需精二（§三）。"""
    if skill_index >= 3:
        return 2
    if skill_index >= 2:
        return 1
    return 0


def _check_op_requirements(
    op_name: str, op_data: dict, owned: Optional[dict], label: str
) -> tuple[Optional[str], Optional[str]]:
    """检查单个干员练度是否满足作业要求。

    返回 (hard_err, risk)：
      - hard_err: 精英/等级硬性不足 → 淘汰（None=通过）
      - risk:     skill_level 盲区/不足 → 仅 ⚠️ 标注，不淘汰（§三）
    """
    if owned is None:
        return None, None

    req = op_data.get("requirements", {}) or {}
    owned_elite = owned.get("elite", 0)
    owned_level = owned.get("level", 0)

    # 精英化：显式 requirements 优先（§评审 P2），否则从 skill 序号隐含推断（§三）。
    req_elite = req.get("elite", 0)
    if req_elite:
        elite_src = f"要求精{req_elite}"
    else:
        req_elite = _infer_elite_from_skill(op_data.get("skill", 0))
        elite_src = f"技能{op_data.get('skill', 0)}隐含精{req_elite}"
    if owned_elite < req_elite:
        return f"{label}: 精{owned_elite}<{elite_src}", None

    # 等级
    req_level = req.get("level", 0)
    if owned_level < req_level:
        return f"{label}: {owned_level}级<要求{req_level}级", None

    # 技能等级/专精：OperBox 盲区（§三）→ 风险标注，不淘汰。
    req_skill = req.get("skill_level", 0)
    if req_skill:
        owned_skill = owned.get("skill_level")
        if owned_skill is None:
            return None, f"{label}: 技能等级未知 (要求{req_skill}, OperBox 盲区)"
        if owned_skill < req_skill:
            return None, f"{label}: 技能{owned_skill}<要求{req_skill}"

    return None, None


def hard_filter(
    item: dict, roster: Optional[Roster]
) -> tuple[bool, list[str], list[str]]:
    """硬过滤（§四 step3, §2.4）。

    - opers[]: 每一名都必须自有且 elite/level 达标；
    - groups[]: 与 opers 不相交，每个 group 是独立槽位 → 需至少一名自有且达标成员；
    - skill_level 盲区走风险通道，不参与淘汰。

    返回 (通过?, 硬失败原因, 风险标注)。
    """
    content = item.get("content_parsed", {}) or {}
    opers = content.get("opers", []) or []
    groups = content.get("groups", []) or []

    if not roster or not roster.owned:
        return True, [], ["⚠️ 无练度数据，未做硬过滤"]

    hard_issues: list[str] = []
    risks: list[str] = []

    # --- opers: 全员校验 ---
    for op in opers:
        op_name = op.get("name", "")
        owned = roster.get(op_name)
        if owned is None:
            hard_issues.append(f"缺干员: {op_name}")
            continue
        hard_err, risk = _check_op_requirements(op_name, op, owned, op_name)
        if hard_err:
            hard_issues.append(hard_err)
        if risk:
            risks.append(risk)

    # --- groups: 每个 group 独立成槽 ---
    for group in groups:
        gname = group.get("name", "?")
        gopers = group.get("opers", []) or []
        slot_ok = False
        slot_risk: Optional[str] = None
        underleveled: list[str] = []
        for gop in gopers:
            g_name = gop.get("name", "") if isinstance(gop, dict) else gop
            g_owned = roster.get(g_name)
            if g_owned is None:
                continue
            g_data = gop if isinstance(gop, dict) else {}
            hard_err, risk = _check_op_requirements(g_name, g_data, g_owned, g_name)
            if hard_err:
                underleveled.append(hard_err)
                continue
            if risk is None:
                slot_ok, slot_risk = True, None  # 干净可用成员，最优 → 收工
                break
            slot_ok = True  # 有风险的可用成员，继续找更干净的
            if slot_risk is None:
                slot_risk = f"分组[{gname}] {risk}"
        if not slot_ok:
            if underleveled:
                hard_issues.append(f"分组[{gname}] 自有成员练度不足: {underleveled[0]}")
            else:
                hard_issues.append(f"分组[{gname}] 无可用干员")
        elif slot_risk:
            risks.append(slot_risk)

    return len(hard_issues) == 0, hard_issues, risks


# =============================================================================
# 软打分（§四 step4）—— 逐字沿用 spike，now 改为注入
# =============================================================================

def requirement_margin(content: dict, roster: Optional[Roster]) -> float:
    """自有 opers 相对作业精英要求的平均裕度（0-1），越高越稳（§四 step4）。"""
    if not roster or not roster.owned:
        return 0.0
    margins = []
    for op in content.get("opers", []) or []:
        owned = roster.get(op.get("name", ""))
        if not owned:
            continue
        req = op.get("requirements", {}) or {}
        req_elite = req.get("elite", 0) or _infer_elite_from_skill(op.get("skill", 0))
        margins.append(max(0.0, min(owned.get("elite", 0) - req_elite, 2) / 2))
    return sum(margins) / len(margins) if margins else 0.0


def soft_score(
    item: dict,
    now: datetime,
    signals: Optional[list[str]] = None,
    margin: float = 0.0,
    risk_count: int = 0,
) -> float:
    """软打分（§四 step4）：**相对排序分**，不是百分制。

    各权重之和通常落在 0~100 附近，但叠加满评级 + 高热度 + 低配信号时可略超 100。
    只用于同一 pass/risk 档内排序，不直接当"XX 分/100"展示给用户（要展示得另做归一）。
    rating_level 权重最高；hot/views 对数归一；like ratio + 新鲜度补充；
    doc 信号（低配友好/高配风险）与练度裕度微调；技能盲区风险扣分。
    """
    score = 0.0

    # rating_level (0-10) → 最大 45 分（社区评级是最可靠的信号）
    rl = item.get("rating_level", 0) or 0
    score += rl * 4.5  # max 45

    # hot_score: log 归一化 → 最大 18 分
    hs = item.get("hot_score", 0) or 0
    if hs > 0:
        score += min(math.log(hs + 1, 10) * 9, 18)

    # views: log 归一化 → 最大 12 分
    views = item.get("views", 0) or 0
    if views > 0:
        score += min(math.log(views + 1, 10) * 4, 12)

    # like ratio → 最大 15 分
    likes = item.get("like", 0) or 0
    dislikes = item.get("dislike", 0) or 0
    total_votes = likes + dislikes
    if total_votes > 0:
        score += (likes / total_votes) * 15

    # 上传新鲜度 → 最大 10 分（30 天内）
    upload = item.get("upload_time", "")
    if upload:
        try:
            try:
                ut = datetime.fromisoformat(upload.replace("Z", "+00:00"))
            except ValueError:
                ut = datetime.fromisoformat(upload)
            if ut.tzinfo is None:
                ut = ut.replace(tzinfo=timezone.utc)
            age_days = (now - ut).days
            # clamp 到 [0,10]：老作业趋 0，未来日期(age 为负)不超过上限。
            score += max(0.0, min(10.0, 10 - age_days * 0.33))
        except ValueError:
            pass

    # doc 信号: 低配友好 +2/个(max6), 高配风险 -3/个(max9)
    if signals:
        green = sum(1 for s in signals if s.startswith("🟢"))
        red = sum(1 for s in signals if s.startswith("🔴"))
        score += min(green * 2, 6)
        score -= min(red * 3, 9)

    # 练度裕度 → 最大 5 分
    score += margin * 5

    # 技能盲区等风险 → 每项 -2
    score -= risk_count * 2

    return round(max(score, 0.0), 1)


# =============================================================================
# Doc 信号解析（默认 analyzer；后续可换 DeepSeek 实现同签名）
# =============================================================================

def analyze_doc(content: dict) -> dict:
    """解析 doc 提取标题、低配/高配关键词信号（启发式，仅供软打分微调）。

    这是 analyzer seam 的默认实现。DeepSeek analyzer 后续实现同样的
    `(content: dict) -> {"title": str, "signals": list[str]}` 契约即可替换。
    """
    doc = content.get("doc", {})
    title = doc.get("title", "") if isinstance(doc, dict) else ""
    details = doc.get("details", "") if isinstance(doc, dict) else ""
    text = f"{title} {details}".lower()

    signals = []
    # 关键词收紧: 去掉易误判的 精一/速通（§评审 P2）。
    low_kw = ["低配", "低练", "无专精", "三级技能", "平民", "养老"]
    high_kw = ["满专", "m3", "高配", "专三", "满潜", "高练度"]
    for kw in low_kw:
        if kw in text:
            signals.append(f"🟢 {kw}")
    for kw in high_kw:
        if kw in text:
            signals.append(f"🔴 {kw}")

    return {"title": title, "signals": signals}


# =============================================================================
# 候选清单
# =============================================================================

@dataclass
class Candidate:
    id: int
    passed: bool
    risky: bool
    score: float
    title: str
    issues: list[str]
    risks: list[str]
    opers: list[str]        # 逐 oper 需求摘要（供 §六① 确认文案）
    groups: list[str]       # 可替换干员组摘要
    doc_signals: list[str]
    difficulty: str
    uploader: str
    upload_time: str
    views: int
    hot_score: float
    rating_level: int
    rating_ratio: float
    likes: int


@dataclass
class CatalogResult:
    stage_display: str
    level_id: str
    collision: list[str]           # 碰撞候选 level_ids（非空=显示号跨活动复用，上层可要求消歧）
    total_fetched: int
    candidates: list[Candidate] = field(default_factory=list)
    # 候选的作业全文（id → content_parsed），供选中后落盘，避免二次下载（§五）。
    # repr=False：内容大，别刷屏日志/repr。
    contents: dict = field(default_factory=dict, repr=False)

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.candidates if c.passed)

    @property
    def pass_clean_count(self) -> int:
        return sum(1 for c in self.candidates if c.passed and not c.risky)


def _oper_summary(content: dict) -> list[str]:
    """逐 oper 需求摘要（名 + 精英/等级/技能要求），供确认文案。"""
    out = []
    for op in content.get("opers", []) or []:
        req = op.get("requirements", {}) or {}
        skill = op.get("skill", 0)
        parts = [f"{op.get('name', '?')}"]
        if req.get("elite"):
            parts.append(f"精{req['elite']}")
        elif skill >= 3:
            parts.append(f"技能{skill}(→精2)")
        elif skill >= 2:
            parts.append(f"技能{skill}(→精1)")
        if req.get("level"):
            parts.append(f"{req['level']}级")
        if req.get("skill_level"):
            parts.append(f"技能{req['skill_level']}")
        out.append(" ".join(parts))
    return out


def _group_summary(content: dict) -> list[str]:
    out = []
    for g in content.get("groups", []) or []:
        names = ", ".join(
            o.get("name", str(o)) if isinstance(o, dict) else str(o)
            for o in (g.get("opers", []) or [])[:3]
        )
        out.append(f"[{g.get('name', '?')}: {names}]")
    return out


def build_candidates(
    stage_display: str,
    roster: Optional[Roster],
    *,
    catalog: dict,
    limit: int = 10,
    rating_min: int = 0,
    level_id_override: Optional[str] = None,
    fetcher: Callable[[str, int], list[dict]] = http_fetch_copilots,
    analyzer: Callable[[dict], dict] = analyze_doc,
    now: Optional[datetime] = None,
) -> CatalogResult:
    """跑完整匹配管线，返回排序后的候选清单。

    排序：通过 > 未通过；通过内 无风险 > 有风险；同档按 soft_score 降序。
    rating_min>0 时，社区评级低于阈值的作业直接丢（软过滤，§十一建议）。
    """
    now = now or datetime.now(timezone.utc)
    level_id, collision = resolve_level_id(catalog, stage_display, level_id_override)
    items = fetcher(level_id, limit)

    candidates: list[Candidate] = []
    contents: dict = {}
    for item in items:
        rating_level = item.get("rating_level", 0) or 0
        if rating_min > 0 and rating_level < rating_min:
            continue

        content = item.get("content_parsed", {}) or {}
        job_id = item.get("id")
        if job_id is not None:
            contents[job_id] = content
        passed, issues, risks = hard_filter(item, roster)
        doc_info = analyzer(content)
        margin = requirement_margin(content, roster)

        candidates.append(
            Candidate(
                id=item.get("id"),
                passed=passed,
                risky=bool(risks),
                score=soft_score(item, now, doc_info.get("signals"), margin, len(risks)),
                title=doc_info.get("title", ""),
                issues=issues if issues else ["✅ 无硬性缺口"],
                risks=risks,
                opers=_oper_summary(content),
                groups=_group_summary(content),
                doc_signals=doc_info.get("signals", []),
                difficulty=content.get("difficulty", ""),
                uploader=item.get("uploader", ""),
                upload_time=item.get("upload_time", ""),
                views=item.get("views", 0) or 0,
                hot_score=item.get("hot_score", 0) or 0,
                rating_level=rating_level,
                rating_ratio=item.get("rating_ratio", 0) or 0,
                likes=item.get("like", 0) or 0,
            )
        )

    candidates.sort(key=lambda c: (c.passed, not c.risky, c.score), reverse=True)
    return CatalogResult(
        stage_display=stage_display,
        level_id=level_id,
        collision=collision,
        total_fetched=len(items),
        candidates=candidates,
        contents=contents,
    )
