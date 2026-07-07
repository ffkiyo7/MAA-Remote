#!/usr/bin/env python3
"""Spike: 构建 stage_id <-> level_id 映射表 (从 prts.plus /arknights/level)。

设计文档 §四 硬前置 — 显示号(FC-EX-2) → level_id 才能调 /copilot/query?level_keyword=。
产出: spikes/fixtures/stage_catalog.json / activity_stage_map.json
"""

import json, os, time, urllib.request

API = "https://prts.maa.plus/arknights/level"
OUT = os.path.join(os.path.dirname(__file__), "fixtures")
os.makedirs(OUT, exist_ok=True)


def main():
    print("[spike:stage_catalog] fetch /arknights/level ...")
    req = urllib.request.Request(API, headers={"User-Agent": "maa-remote-spike"})
    with urllib.request.urlopen(req, timeout=60) as r:
        levels = json.loads(r.read())["data"]
    print(f"  {len(levels)} levels")

    # 多向映射
    display_to_level = {}   # 显示号 -> level_id
    stage_to_level = {}     # stage_id -> level_id
    act_stages = {}         # 活动关卡: 显示号 -> level_id
    act_level_to_disp = {}  # 活动关卡: level_id -> 显示号

    for lv in levels:
        lid, sid, ct = lv["level_id"], lv["stage_id"], lv["cat_three"]
        stage_to_level[sid] = lid
        display_to_level.setdefault(ct, []).append(lid)
        if lv["cat_one"] == "活动关卡":
            act_stages[ct] = lid
            act_level_to_disp[lid] = ct

    # 去重 (优先非 #f#；记录真实碰撞 — 指向多个不同 level_id 的复用显示号)
    dedup = {}
    collision_map = {}  # 显示号 -> [level_id, ...]  供 copilot_catalog 消歧告警
    for ct, lids in display_to_level.items():
        normal = [l for l in lids if "#f#" not in l]
        # 去掉指向同一 level_id 的重复, 只有真正多目标才算碰撞 (§评审 P1)
        distinct = list(dict.fromkeys(normal))
        if len(distinct) > 1:
            collision_map[ct] = distinct
        dedup[ct] = normal[0] if normal else lids[0]
    if collision_map:
        print(f"  ⚠️  去重碰撞 ({len(collision_map)} 个显示号跨活动复用, 指向多个 level_id):")
        for ct, distinct in list(collision_map.items())[:5]:
            print(f"    {ct}: {distinct} → 默认 {dedup[ct]} (下游可 --level-id 消歧)")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    full = {"fetched_at": now, "total": len(levels),
            "display_to_level": dedup, "stage_to_level": stage_to_level,
            "display_collisions": collision_map,
            "activity_stages": act_stages, "activity_level_to_display": act_level_to_disp}
    with open(os.path.join(OUT, "stage_catalog.json"), "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)

    light = {"fetched_at": now, "total": len(act_stages), "map": act_stages}
    with open(os.path.join(OUT, "activity_stage_map.json"), "w", encoding="utf-8") as f:
        json.dump(light, f, ensure_ascii=False, indent=2)

    print(f"  stage_catalog.json: {os.path.getsize(os.path.join(OUT, 'stage_catalog.json'))} bytes")
    print(f"  activity_stage_map.json: {os.path.getsize(os.path.join(OUT, 'activity_stage_map.json'))} bytes")
    for ct in ["FC-EX-2", "1-7", "LS-5"]:
        print(f"  {ct} -> {dedup.get(ct, 'N/A')}")


if __name__ == "__main__":
    main()
