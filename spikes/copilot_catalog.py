#!/usr/bin/env python3
"""Spike: copilot_catalog 原型 — prts.plus 查询 → 解析 → 硬过滤 → 软打分。

设计文档 §四 的完整匹配管线：
  1. 关卡定位: 显示号 → level_id (依赖 build_stage_catalog.py 产出的 stage_catalog.json)
  2. 查询: /copilot/query?level_keyword= + orderBy=hot
  3. 硬过滤: opers/groups 全自有干员满足。包含 skill→elite 隐含推断 (§三)，
     groups 替补也检查练度达标 (§四 step3)。
  4. 软打分: rating_level + hot_score + views + requirements 裕度 + doc 信号
  5. 产出: 候选清单

使用: python spikes/copilot_catalog.py <显示号> [--limit N] [--roster roster.json]
示例: python spikes/copilot_catalog.py FC-EX-2
       python spikes/copilot_catalog.py 1-7 --limit 20 --roster my_box.json

产出:
  spikes/fixtures/copilot_candidates_<stage>.json — 候选清单
  spikes/fixtures/copilot_raw_<stage>.json       — 原始 API 响应
"""

import json, math, os, sys, time, urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

QUERY_API = "https://prts.maa.plus/copilot/query"
UA = "maa-remote-spike"
OUT = os.path.join(os.path.dirname(__file__), "fixtures")


# =============================================================================
# Roster (练度数据)
# =============================================================================

@dataclass
class Roster:
    """干员练度数据。真实场景由 OperBox/Skland 填充。"""
    owned: dict = field(default_factory=dict)  # {op_name: {elite, level, skill_level, module}}

    @classmethod
    def mock(cls):
        """一份宽裕的假 Box (6★ 精二 50-60 级常见干员)"""
        return cls(owned={
            "山": {"elite": 2, "level": 60, "skill_level": 7},
            "银灰": {"elite": 2, "level": 50, "skill_level": 7},
            "艾雅法拉": {"elite": 2, "level": 60, "skill_level": 7},
            "能天使": {"elite": 2, "level": 60, "skill_level": 7},
            "塞雷娅": {"elite": 2, "level": 60, "skill_level": 7},
            "星熊": {"elite": 2, "level": 40, "skill_level": 7},
            "推进之王": {"elite": 2, "level": 40, "skill_level": 7},
            "夜莺": {"elite": 2, "level": 40, "skill_level": 7},
            "闪灵": {"elite": 2, "level": 40, "skill_level": 7},
            "安洁莉娜": {"elite": 2, "level": 40, "skill_level": 7},
            "桃金娘": {"elite": 2, "level": 40, "skill_level": 7},
            "蛇屠箱": {"elite": 2, "level": 40, "skill_level": 7},
            "克洛丝": {"elite": 1, "level": 55, "skill_level": 7},
            "芬": {"elite": 1, "level": 55, "skill_level": 7},
            "玫兰莎": {"elite": 1, "level": 55, "skill_level": 7},
        })

    def get(self, name: str) -> Optional[dict]:
        return self.owned.get(name)


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
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())

    items = []
    for item in resp.get("data", {}).get("data", []):
        content = item.get("content", "")
        if isinstance(content, str):
            try:
                item["content_parsed"] = json.loads(content)
            except json.JSONDecodeError:
                item["content_parsed"] = {}
        else:
            item["content_parsed"] = content
        items.append(item)
    return items


# =============================================================================
# 硬过滤
# =============================================================================

def _infer_elite_from_skill(skill_index: int) -> int:
    """skill→精英隐含要求：2技能需精一，3技能需精二 (§三)"""
    if skill_index >= 3:
        return 2
    if skill_index >= 2:
        return 1
    return 0


def _check_op_requirements(
    op_name: str, op_data: dict, owned: Optional[dict], label: str
) -> Optional[str]:
    """检查单个干员的练度是否满足作业要求。返回 None=通过, str=失败原因。"""
    if owned is None:
        return None

    # 显式 requirements
    req = op_data.get("requirements", {})
    if req:
        req_elite = req.get("elite", 0)
        if owned.get("elite", 0) < req_elite:
            return f"{label}: 精{owned['elite']}<要求精{req_elite}"
        req_level = req.get("level", 0)
        if owned.get("level", 0) < req_level:
            return f"{label}: {owned['level']}级<要求{req_level}级"
        req_skill = req.get("skill_level", 0)
        if req_skill and owned.get("skill_level", 0) < req_skill:
            return f"{label}: 技能{owned['skill_level']}<要求{req_skill} (⚠️ 盲区)"

    # 无 requirements → skill→elite 隐含推断 (§三)
    skill_idx = op_data.get("skill", 0)
    inferred = _infer_elite_from_skill(skill_idx)
    if inferred > 0 and owned.get("elite", 0) < inferred:
        return f"{label}: 精{owned['elite']}<技能{skill_idx}隐含精{inferred}"

    return None


def hard_filter(item: dict, roster: Optional[Roster]) -> tuple[bool, list[str]]:
    """硬过滤：检查所有 opers 和 groups 能否被自有干员满足。

    设计文档 §四 step3 + §三 skill→elite 推断。
    groups 中任一成员的练度达标即该槽位通过。

    返回 (通过?, 原因列表)。
    """
    content = item.get("content_parsed", {})
    opers = content.get("opers", [])
    groups = content.get("groups", [])

    if not roster or not roster.owned:
        return True, ["⚠️ 无练度数据，未做硬过滤"]

    issues: list[str] = []

    # groups 索引: op_name → (group_index, op_data)
    group_membership: dict[str, tuple[int, dict]] = {}
    for gi, group in enumerate(groups):
        for gop in group.get("opers", []):
            if isinstance(gop, dict):
                group_membership[gop.get("name", "")] = (gi, gop)
            elif isinstance(gop, str):
                group_membership[gop] = (gi, {})

    satisfied_groups: set[int] = set()

    for op in opers:
        op_name = op.get("name", "")
        owned = roster.get(op_name)

        if owned:
            # 有该干员 → 检查练度
            err = _check_op_requirements(op_name, op, owned, op_name)
            if err:
                issues.append(err)
        else:
            # 没有该干员 → 查 groups
            if op_name in group_membership:
                gi, _ = group_membership[op_name]
                if gi in satisfied_groups:
                    continue
                # 遍历 group 内所有替补，找第一个练度达标的
                group_opers = groups[gi].get("opers", [])
                found = False
                for gop in group_opers:
                    g_name = gop.get("name", "") if isinstance(gop, dict) else gop
                    g_owned = roster.get(g_name)
                    if g_owned:
                        err = _check_op_requirements(g_name, gop if isinstance(gop, dict) else {}, g_owned, f"{g_name}(替{op_name})")
                        if err is None:
                            satisfied_groups.add(gi)
                            found = True
                            break
                if not found:
                    issues.append(f"缺干员: {op_name} (无可用的替补)")
                continue
            issues.append(f"缺干员: {op_name}")

    return len(issues) == 0, issues if issues else ["✅ 全部满足"]


# =============================================================================
# 软打分
# =============================================================================

def soft_score(item: dict) -> float:
    """软打分：0-100。rating_level 权重最高，hot/views 对数归一，like ratio + 新鲜度补充。

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

    return round(score, 1)


# =============================================================================
# Doc 信号解析
# =============================================================================

def analyze_doc(content: dict) -> dict:
    """解析 doc 提取标题、风险/亮点关键词。"""
    doc = content.get("doc", {})
    title = doc.get("title", "") if isinstance(doc, dict) else ""
    details = doc.get("details", "") if isinstance(doc, dict) else ""
    text = f"{title} {details}".lower()

    signals = []
    low_kw = ["低配", "低练", "无专精", "三级技能", "精一", "平民"]
    high_kw = ["满专", "m3", "高配", "速通", "专三", "满潜"]
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
    if len(sys.argv) < 2:
        sys.exit("用法: python copilot_catalog.py <显示号> [--limit N] [--roster roster.json]")
    stage_display = sys.argv[1]
    limit = 10
    roster_path = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--roster" and i + 1 < len(args):
            roster_path = args[i + 1]
            i += 2
        else:
            i += 1

    # 1. 关卡定位
    catalog = load_stage_catalog()
    level_id = catalog["display_to_level"].get(stage_display)
    if not level_id:
        print(f"❌ 未知关卡显示号: {stage_display}")
        sys.exit(1)
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
        passed, issues = hard_filter(item, roster)

        # oper 需求摘要
        oper_reqs = []
        for op in content.get("opers", []):
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
        for g in content.get("groups", []):
            names = ", ".join(o.get("name", str(o)) if isinstance(o, dict) else str(o)
                              for o in g.get("opers", [])[:3])
            group_reqs.append(f"[{g.get('name', '?')}: {names}]")

        doc_info = analyze_doc(content)

        candidates.append({
            "id": item["id"],
            "pass": passed,
            "issues": issues,
            "score": soft_score(item),
            "uploader": item.get("uploader", ""),
            "upload_time": item.get("upload_time", ""),
            "views": item.get("views", 0),
            "hot_score": item.get("hot_score", 0),
            "rating_level": item.get("rating_level", 0),
            "rating_ratio": item.get("rating_ratio", 0),
            "likes": item.get("like", 0),
            "title": doc_info["title"],
            "opers": oper_reqs,
            "groups": group_reqs,
            "doc_signals": doc_info["signals"],
            "difficulty": content.get("difficulty", ""),
        })

    # 排序: 通过 > 未通过; 同组按 score 降序
    candidates.sort(key=lambda c: (c["pass"], c["score"]), reverse=True)

    # 5. 输出
    print(f"\n{'='*70}")
    print(f"  候选清单: {stage_display}  ({roster_used} Box)")
    print(f"{'='*70}")
    for i, c in enumerate(candidates):
        icon = "✅" if c["pass"] else "❌"
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
            "candidates": candidates,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 完整结果 -> {out_path}")
    print(f"  💾 原始数据 -> {raw_path}")


if __name__ == "__main__":
    main()
