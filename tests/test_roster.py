import json

from maa_remote.roster import Roster


def test_get_exact_name():
    r = Roster.mock()
    assert r.get("山") == {"elite": 2, "level": 60}


def test_get_normalizes_skin_suffix():
    # 皮肤名 = 前缀 + 规范名，规范名在词尾 → 凛御银灰 归一到 银灰。
    r = Roster.mock()
    assert r.get("凛御银灰") == r.get("银灰")


def test_get_skin_alias_special_case():
    # 单字规范名靠后缀启发式无法安全归一 → SKIN_ALIASES 特例表。
    r = Roster.mock()
    assert r.get("历阵锐枪芬") == r.get("芬")


def test_get_unknown_returns_none():
    assert Roster.mock().get("不存在的干员") is None


def test_is_empty():
    assert Roster().is_empty() is True
    assert Roster.mock().is_empty() is False


def test_load_full_shape(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text(
        json.dumps(
            {"source": "operbox", "fetched_at": "2026-07-08T00:00:00Z",
             "owned": {"山": {"elite": 2, "level": 60}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    r = Roster.load(str(p))
    assert r.source == "operbox"
    assert r.fetched_at == "2026-07-08T00:00:00Z"
    assert r.get("山") == {"elite": 2, "level": 60}


def test_load_bare_owned_dict(tmp_path):
    # 兼容手写的裸 owned dict（无元数据）。
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"银灰": {"elite": 2, "level": 50}}), encoding="utf-8")
    r = Roster.load(str(p))
    assert r.source == ""
    assert r.get("银灰") == {"elite": 2, "level": 50}
