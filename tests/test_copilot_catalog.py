import json
import os
from datetime import datetime, timezone

import pytest

from maa_remote import copilot_catalog as cc
from maa_remote.copilot_catalog import (
    CopilotFetchError,
    StageResolutionError,
    build_candidates,
    resolve_level_id,
)
from maa_remote.roster import Roster

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "copilot_query_1-7.json")
NOW = datetime(2026, 7, 8, tzinfo=timezone.utc)

CATALOG = {
    # 碰撞显示号在 display_to_level 里仍有一个默认 level_id（build_stage_catalog 的实际形状）。
    "display_to_level": {"1-7": "obt/main/level_main_01-07", "TN-1": "act/a/tn1"},
    "display_collisions": {"TN-1": ["act/a/tn1", "act/b/tn1"]},
}


def _fake_fetcher():
    """离线 fetcher：从 fixture 读真实 /copilot/query 形状并走生产解析。"""
    with open(FIXTURE, encoding="utf-8") as f:
        resp = json.load(f)

    def fetch(level_id, limit):
        return cc._parse_query_rows(resp)

    return fetch


# ---------------------------------------------------------------------------
# 关卡定位
# ---------------------------------------------------------------------------

def test_resolve_level_id_basic():
    level_id, collision = resolve_level_id(CATALOG, "1-7")
    assert level_id == "obt/main/level_main_01-07"
    assert collision == []


def test_resolve_level_id_unknown_raises():
    with pytest.raises(StageResolutionError):
        resolve_level_id(CATALOG, "ZZ-9")


def test_resolve_level_id_collision_returns_candidates():
    level_id, collision = resolve_level_id(CATALOG, "TN-1")
    assert collision == ["act/a/tn1", "act/b/tn1"]


def test_resolve_level_id_override_wins():
    level_id, collision = resolve_level_id(CATALOG, "TN-1", override="act/b/tn1")
    assert level_id == "act/b/tn1"
    assert collision == []


# ---------------------------------------------------------------------------
# 硬过滤 + 打分 + 排序（fixture 覆盖 净通过/缺干员/技能盲区/分组槽）
# ---------------------------------------------------------------------------

def test_build_candidates_pass_counts_and_order():
    res = build_candidates(
        "1-7", Roster.mock(), catalog=CATALOG, fetcher=_fake_fetcher(), now=NOW
    )
    assert res.level_id == "obt/main/level_main_01-07"
    assert res.total_fetched == 4
    assert res.pass_count == 3        # 1001, 1004, 1003
    assert res.pass_clean_count == 2  # 1001, 1004
    # 排序：净通过 > 带风险 > 未通过；净通过内按 score。
    assert [c.id for c in res.candidates] == [1001, 1004, 1003, 1002]


def test_missing_oper_eliminated():
    res = build_candidates(
        "1-7", Roster.mock(), catalog=CATALOG, fetcher=_fake_fetcher(), now=NOW
    )
    c1002 = next(c for c in res.candidates if c.id == 1002)
    assert c1002.passed is False
    assert any("缺干员" in i for i in c1002.issues)


def test_skill_level_is_risk_not_elimination():
    # OperBox 盲区：skill_level 要求 → 风险标注，不淘汰（§三）。
    res = build_candidates(
        "1-7", Roster.mock(), catalog=CATALOG, fetcher=_fake_fetcher(), now=NOW
    )
    c1003 = next(c for c in res.candidates if c.id == 1003)
    assert c1003.passed is True
    assert c1003.risky is True
    assert any("技能等级未知" in r for r in c1003.risks)


def test_group_slot_satisfied_by_owned_member():
    # opers 空 + 一个 group（陈缺，艾雅法拉自有且干净）→ 独立成槽通过，净通过。
    res = build_candidates(
        "1-7", Roster.mock(), catalog=CATALOG, fetcher=_fake_fetcher(), now=NOW
    )
    c1004 = next(c for c in res.candidates if c.id == 1004)
    assert c1004.passed is True
    assert c1004.risky is False


def test_rating_min_soft_filters():
    # rating_min=7 丢掉 1004(rating6)，保留 1001/1003/1002。
    res = build_candidates(
        "1-7", Roster.mock(), catalog=CATALOG, fetcher=_fake_fetcher(),
        now=NOW, rating_min=7,
    )
    ids = {c.id for c in res.candidates}
    assert ids == {1001, 1003, 1002}
    assert len(res.candidates) == 3
    assert res.total_fetched == 4  # 拉取 4 份，rating_min 软过滤后剩 3 份候选


def test_no_roster_skips_hard_filter():
    # 无练度数据 → 不淘汰，全部标风险"未做硬过滤"。
    res = build_candidates(
        "1-7", Roster(), catalog=CATALOG, fetcher=_fake_fetcher(), now=NOW
    )
    assert res.pass_count == 4
    assert all(c.risky for c in res.candidates)


def test_analyzer_seam_is_injectable():
    # 换 analyzer 不改管线：验证注入的实现被调用。
    def fake_analyzer(content):
        return {"title": "SEAM", "signals": ["🟢 注入"]}

    res = build_candidates(
        "1-7", Roster.mock(), catalog=CATALOG, fetcher=_fake_fetcher(),
        now=NOW, analyzer=fake_analyzer,
    )
    assert all(c.title == "SEAM" for c in res.candidates)


def test_oper_summary_present_for_confirm_message():
    res = build_candidates(
        "1-7", Roster.mock(), catalog=CATALOG, fetcher=_fake_fetcher(), now=NOW
    )
    c1001 = next(c for c in res.candidates if c.id == 1001)
    assert any("山" in s for s in c1001.opers)


# ---------------------------------------------------------------------------
# 查询响应校验（业务失败 ≠ 无候选）
# ---------------------------------------------------------------------------

def test_parse_query_rows_success_200():
    resp = {"status_code": 200, "data": {"data": [{"id": 1, "content": "{}"}]}}
    rows = cc._parse_query_rows(resp)
    assert len(rows) == 1
    assert rows[0]["content_parsed"] == {}


def test_parse_query_rows_missing_status_is_lenient():
    # status_code 缺省 → 从宽放行。
    resp = {"data": {"data": []}}
    assert cc._parse_query_rows(resp) == []


def test_parse_query_rows_business_failure_raises():
    resp = {"status_code": 500, "data": {"data": []}}
    with pytest.raises(CopilotFetchError):
        cc._parse_query_rows(resp)


def test_parse_query_rows_bad_data_shape_raises():
    resp = {"status_code": 200, "data": None}
    with pytest.raises(CopilotFetchError):
        cc._parse_query_rows(resp)


def test_parse_query_rows_top_level_not_dict_raises():
    with pytest.raises(CopilotFetchError):
        cc._parse_query_rows([])


def test_soft_score_future_upload_freshness_clamped():
    # 未来日期上传（age 为负）新鲜度被 clamp 到上限 10，不爆表。
    # 只有 rating_level=0、无 hot/views/like → 分数 = 仅新鲜度项 = 10.0。
    future = {"rating_level": 0, "upload_time": "2099-01-01T00:00:00Z"}
    assert cc.soft_score(future, NOW) == 10.0


# ---------------------------------------------------------------------------
# Live smoke（prts 真网络，opt-in；默认跳过，不进主 CI）
#   RUN_LIVE_SMOKE=1 python -m pytest tests/test_copilot_catalog.py -k live
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_SMOKE"),
    reason="live smoke 需真网络；设 RUN_LIVE_SMOKE=1 开启",
)
def test_live_smoke_http_fetch_main_1_7():
    # 主线 1-7 内部名 main_01-07，子串匹配（§十一 S1-API 已定案）。
    items = cc.http_fetch_copilots("main_01-07", limit=5)
    assert len(items) > 0
    first = items[0]
    assert "content_parsed" in first
    assert isinstance(first["content_parsed"], dict)
