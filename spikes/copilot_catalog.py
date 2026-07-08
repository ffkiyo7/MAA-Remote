#!/usr/bin/env python3
"""Spike: copilot_catalog 原型 — prts.plus 查询 → 解析 → 硬过滤 → 软打分。

设计文档 §四 的完整匹配管线：
  1. 关卡定位: 显示号 → level_id (依赖 build_stage_catalog.py 产出的 stage_catalog.json)。
     碰撞显示号(跨活动复用)会告警, 支持 --level-id 消歧 (§四 step1)。
  2. 查询: /copilot/query?level_keyword= + orderBy=hot
  3. 硬过滤 (§四 step3, §2.4):
       - opers[] 每一名都必须自有且 elite/level 达标 (缺人/练度不足 → 淘汰);
       - groups[] 与 opers 不相交, 每个 group 是独立槽位 → 至少一名自有且达标成员;
       - skill_level 是 OperBox 盲区 (§三) → 仅产出 ⚠️ 风险标注, 不参与淘汰;
       - 无显式 elite requirement 时才从 skill 序号隐含推断 (§三)。
  4. 软打分 (§四 step4): rating_level + hot_score + views + like ratio + 新鲜度
       + doc 信号 + 练度裕度 - 风险。
  5. 产出: 候选清单 (通过且无风险 > 通过但有风险 > 未通过)。

使用: python spikes/copilot_catalog.py <显示号> [--limit N] [--roster roster.json] [--level-id LID]
示例: python spikes/copilot_catalog.py FC-EX-2
       python spikes/copilot_catalog.py 1-7 --limit 20 --roster my_box.json

产出:
  spikes/fixtures/copilot_candidates_<stage>.json — 候选清单
  spikes/fixtures/copilot_raw_<stage>.json       — 原始 API 响应
"""

import json, math, os, sys, time, urllib.error, urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

QUERY_API = "https://prts.maa.plus/copilot/query"
UA = "maa-remote-spike"
OUT = os.path.join(os.path.dirname(__file__), "fixtures")


def configure_console_encoding() -> None:
    """Keep Windows GBK consoles from crashing on emoji/CJK spike output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


# =============================================================================
# 皮肤别名 (§评审 P1: 作业里干员常带皮肤前缀, 需归一到规范名)
# =============================================================================

# 多字规范名的皮肤 (凛御银灰/纯烬艾雅法拉/斩业星熊 …) 由 Roster 的后缀归一自动覆盖;
# 这里只补单字规范名等后缀启发式无法安全归一的特例。可按需扩充 (MAA 自身识别会归一)。
SKIN_ALIASES = {
    "历阵锐枪芬": "芬",
}


# =============================================================================
# Roster (练度数据)
# =============================================================================

@dataclass
class Roster:
    """干员练度数据。真实场景由 OperBox/Skland 填充。

    注意: 真实 OperBox 回调不含 skill_level (§2.4) → 缺省即"无数据",
    匹配层据此走风险标注而非淘汰。
    """
    owned: dict = field(default_factory=dict)  # {规范名: {elite, level, skill_level?, module?}}

    @classmethod
    def mock(cls):
        """一份中等偏上的假 Box (6★ 精二常见干员)。

        刻意不含 skill_level —— 与真实 OperBox 一致 (§2.4), 用于暴露技能盲区路径。
        """
        return cls(owned={
            "山": {"elite": 2, "level": 60},
            "银灰": {"elite": 2, "level": 50},
            "艾雅法拉": {"elite": 2, "level": 60},
            "能天使": {"elite": 2, "level": 60},
            "塞雷娅": {"elite": 2, "level": 60},
            "星熊": {"elite": 2, "level": 40},
            "推进之王": {"elite": 2, "level": 40},
            "夜莺": {"elite": 2, "level": 40},
            "闪灵": {"elite": 2, "level": 40},
            "安洁莉娜": {"elite": 2, "level": 40},
            "桃金娘": {"elite": 2, "level": 40},
            "蛇屠箱": {"elite": 2, "level": 40},
            "克洛丝": {"elite": 1, "level": 55},
            "芬": {"elite": 1, "level": 55},
            "玫兰莎": {"elite": 1, "level": 55},
        })

    def get(self, name: str) -> Optional[dict]:
        """按干员名取练度, 自动归一皮肤别名。"""
        if name in self.owned:
            return self.owned[name]
        canon = self._canonical(name)
        if canon is not None:
            return self.owned.get(canon)
        return None

    def _canonical(self, name: str) -> Optional[str]:
        """把皮肤名归一到自有的规范名, 归一不到返回 None。"""
        alias = SKIN_ALIASES.get(name)
        if alias and alias in self.owned:
            return alias
        # 皮肤名 = 装饰前缀 + 规范名, 规范名总在词尾。取最长的自有后缀 (>=2 字避免误配)。
        best = None
        for k in self.owned:
            if len(k) >= 2 and name != k and name.endswith(k):
                if best is None or len(k) > len(best):
                    best = k
        return best


# =============================================================================
# Stage Catalog (关卡映射，依赖 build_stage_catalog.py 产出)
# =============================================================================

def load_stage_catalog() -> dict:
    """加载关卡映射表。如果没有，提示先跑 build_stage_catalog.py。"""
    cat_path = os.path.join(OUT, "stage_catalog.json")
    if not os.path.exists(cat_path):
        sys.exit(
            f"❌ 未找到 {cat_path}\n"
            f"   请先运行: python spikes/build_stage_catalog.py"
        )
    with open(cat_path, encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# API 查询
# =============================================================================

def fetch_copilots(level_id: str, limit: int = 10) -> list:
    """查询作业列表，返回 items (content 已解析为 content_parsed)。"""
    params = f"level_keyword={level_id}&orderBy=hot&page=1&limit={limit}"
    url = f"{QUERY_API}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"❌ 查询失败 HTTP {e.code}: {url}")
    except urllib.error.URLError as e:
        sys.exit(f"❌ 网络错误: {e.reason}")
    except json.JSONDecodeError:
        sys.exit("❌ 查询响应非合法 JSON")

    data = resp.get("data") or {}
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
# 硬过滤
# =============================================================================

def _infer_elite_from_skill(skill_index: int) -> int:
    """skill→精英隐含要求：2技能需精一，3技能需精二 (§三)。"""
    if skill_index >= 3:
        return 2
    if skill_index >= 2:
        return 1
    return 0


def _check_op_requirements(
    op_name: str, op_data: dict, owned: Optional[dict], label: str
) -> tuple[Optional[str], Optional[str]]:
    """检查单个干员练度是否满足作业要求。

    返回 (hard_err, risk):
      - hard_err: 精英/等级硬性不足 → 淘汰 (None=通过)
      - risk:     skill_level 盲区/不足 → 仅 ⚠️ 标注, 不淘汰 (§三)
    """
    if owned is None:
        return None, None

    req = op_data.get("requirements", {}) or {}
    owned_elite = owned.get("elite", 0)
    owned_level = owned.get("level", 0)

    # 精英化: 显式 requirements 优先 (§评审 P2), 否则从 skill 序号隐含推断 (§三)。
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

    # 技能等级/专精: OperBox 盲区 (§三) → 风险标注, 不淘汰。
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
    """硬过滤 (§四 step3, §2.4)。

    - opers[]: 每一名都必须自有且 elite/level 达标;
    - groups[]: 与 opers 不相交, 每个 group 是独立槽位 → 需至少一名自有且达标成员;
    - skill_level 盲区走风险通道, 不参与淘汰。

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
                slot_ok, slot_risk = True, None   # 干净可用成员, 最优 → 收工
                break
            slot_ok = True                         # 有风险的可用成员, 继续找更干净的
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
# 软打分
# =============================================================================

def requirement_margin(content: dict, roster: Optional[Roster]) -> float:
    """自有 opers 相对作业精英要求的平均裕度 (0-1)，越高越稳 (§四 step4)。"""
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
    item: dict, signals: Optional[list[str]] = None,
    margin: float = 0.0, risk_count: int = 0,
) -> float:
    """软打分：0-100 (§四 step4)。

    rating_level 权重最高; hot/views 对数归一; like ratio + 新鲜度补充;
    doc 信号(低配友好/高配风险) 与 练度裕度 微调; 技能盲区风险扣分。

    注意：hot_score 和 views 大跨度 (hot 可达千级, 浏览可达万级)，
    对数系数经标定确保热度区间的区分度，热门作业不会全满分。
    """
    score = 0.0

    # rating_level (0-10) → 最大 45 分 (社区评级是最可靠的信号)
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

    # 上传新鲜度 → 最大 10 分 (30 天内)
    upload = item.get("upload_time", "")
    if upload:
        try:
            try:
                ut = datetime.fromisoformat(upload.replace("Z", "+00:00"))
            except ValueError:
                ut = datetime.fromisoformat(upload)
            if ut.tzinfo is None:
                ut = ut.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ut).days
            score += max(0, 10 - age_days * 0.33)  # 30天内的作业加分
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
# Doc 信号解析
# =============================================================================

def analyze_doc(content: dict) -> dict:
    """解析 doc 提取标题、低配/高配关键词信号 (启发式, 仅供软打分微调)。"""
    doc = content.get("doc", {})
    title = doc.get("title", "") if isinstance(doc, dict) else ""
    details = doc.get("details", "") if isinstance(doc, dict) else ""
    text = f"{title} {details}".lower()

    signals = []
    # 关键词收紧: 去掉易误判的 精一/速通 (§评审 P2)。
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
# Main
# =============================================================================

def main():
    configure_console_encoding()
    if len(sys.argv) < 2:
        sys.exit("用法: python copilot_catalog.py <显示号> [--limit N] [--roster roster.json] [--level-id LID]")
    stage_display = sys.argv[1]
    limit = 10
    roster_path = None
    override_level_id = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--roster" and i + 1 < len(args):
            roster_path = args[i + 1]
            i += 2
        elif args[i] == "--level-id" and i + 1 < len(args):
            override_level_id = args[i + 1]
            i += 2
        else:
            i += 1

    # 1. 关卡定位 (碰撞显示号需消歧, §四 step1 / §评审 P1)
    catalog = load_stage_catalog()
    collisions = catalog.get("display_collisions", {})
    if override_level_id:
        level_id = override_level_id
        print(f"[关卡定位] {stage_display} -> {level_id} (--level-id 指定)")
    else:
        level_id = catalog["display_to_level"].get(stage_display)
        if not level_id:
            print(f"❌ 未知关卡显示号: {stage_display}")
            sys.exit(1)
        if stage_display in collisions:
            print(f"⚠️ 显示号碰撞: {stage_display} 跨活动复用, 候选 level_id:")
            for lid in collisions[stage_display]:
                print(f"     {lid}")
            print(f"   默认选用 {level_id}; 打新活动关请用 --level-id 指定当期。")
        else:
            print(f"[关卡定位] {stage_display} -> {level_id}")

    # 2. 查询
    items = fetch_copilots(level_id, limit)
    print(f"[查询] {len(items)} 份作业 (level_keyword={level_id})")

    # 保存原始响应
    raw_path = os.path.join(OUT, f"copilot_raw_{stage_display}.json")
    raw_out = [{"id": it["id"], "uploader": it.get("uploader"), "views": it.get("views"),
                "hot_score": it.get("hot_score"), "rating_level": it.get("rating_level"),
                "rating_ratio": it.get("rating_ratio"), "content": it.get("content_parsed")}
               for it in items]
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw_out, f, ensure_ascii=False, indent=2)

    # 3. 加载练度: --roster 指定 > mock 默认
    roster_used = "mock"
    if roster_path:
        with open(roster_path, encoding="utf-8") as f:
            roster = Roster(owned=json.load(f))
        roster_used = roster_path
    else:
        roster = Roster.mock()

    # 4. 硬过滤 + 软打分
    candidates = []
    for item in items:
        content = item["content_parsed"]
        passed, issues, risks = hard_filter(item, roster)

        # oper 需求摘要
        oper_reqs = []
        for op in content.get("opers", []) or []:
            req = op.get("requirements", {}) or {}
            skill = op.get("skill", 0)
            parts = [f"{op.get('name','?')}"]
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
            oper_reqs.append(" ".join(parts))

        # groups 摘要
        group_reqs = []
        for g in content.get("groups", []) or []:
            names = ", ".join(o.get("name", str(o)) if isinstance(o, dict) else str(o)
                              for o in (g.get("opers", []) or [])[:3])
            group_reqs.append(f"[{g.get('name', '?')}: {names}]")

        doc_info = analyze_doc(content)
        margin = requirement_margin(content, roster)

        candidates.append({
            "id": item["id"],
            "pass": passed,
            "risky": bool(risks),
            "issues": issues if issues else ["✅ 无硬性缺口"],
            "risks": risks,
            "score": soft_score(item, doc_info["signals"], margin, len(risks)),
            "uploader": item.get("uploader", ""),
            "upload_time": item.get("upload_time", ""),
            "views": item.get("views", 0) or 0,
            "hot_score": item.get("hot_score", 0) or 0,
            "rating_level": item.get("rating_level", 0) or 0,
            "rating_ratio": item.get("rating_ratio", 0) or 0,
            "likes": item.get("like", 0) or 0,
            "title": doc_info["title"],
            "opers": oper_reqs,
            "groups": group_reqs,
            "doc_signals": doc_info["signals"],
            "difficulty": content.get("difficulty", ""),
        })

    # 排序: 通过 > 未通过; 通过内 无风险 > 有风险; 同档按 score 降序
    candidates.sort(key=lambda c: (c["pass"], not c["risky"], c["score"]), reverse=True)

    # 5. 输出
    print(f"\n{'='*70}")
    print(f"  候选清单: {stage_display}  ({roster_used} Box)")
    print(f"{'='*70}")
    for i, c in enumerate(candidates):
        if not c["pass"]:
            icon = "❌"
        elif c["risky"]:
            icon = "⚠️"
        else:
            icon = "✅"
        title_line = f"「{c['title']}」" if c["title"] else ""
        print(f"\n  [{i+1}] {icon} #{c['id']} {title_line}  score={c['score']}  rating={c['rating_level']}★")
        print(f"      上传: {c['uploader']}  浏览: {c['views']}  hot: {c['hot_score']:.1f}")
        print(f"      编队: {', '.join(c['opers'][:6])}")
        if c["groups"]:
            print(f"      替换: {', '.join(c['groups'][:3])}")
        if c["doc_signals"]:
            print(f"      信号: {', '.join(c['doc_signals'])}")
        if c["difficulty"]:
            print(f"      难度: {c['difficulty']}")
        print(f"      过滤: {'; '.join(c['issues'][:3])}")
        if c["risks"]:
            print(f"      风险: {'; '.join(c['risks'][:3])}")

    # 保存
    out_path = os.path.join(OUT, f"copilot_candidates_{stage_display}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "stage": stage_display,
            "level_id": level_id,
            "roster": roster_used,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_fetched": len(items),
            "pass_count": sum(1 for c in candidates if c["pass"]),
            "pass_clean_count": sum(1 for c in candidates if c["pass"] and not c["risky"]),
            "candidates": candidates,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 完整结果 -> {out_path}")
    print(f"  💾 原始数据 -> {raw_path}")


if __name__ == "__main__":
    main()
