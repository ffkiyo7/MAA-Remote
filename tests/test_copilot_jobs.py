import json
import types

import pytest

from maa_remote.copilot_catalog import CatalogResult
from maa_remote.copilot_jobs import persist_content, persist_from_result, resolve_jobs_dir


def _cfg(jobs_dir="", config_dir="D:/cfg"):
    return types.SimpleNamespace(
        copilot=types.SimpleNamespace(jobs_dir=jobs_dir),
        maa=types.SimpleNamespace(config_dir=config_dir),
    )


def test_resolve_jobs_dir_defaults_under_config_dir():
    import os

    got = resolve_jobs_dir(_cfg(jobs_dir="", config_dir="D:/cfg"))
    assert got == os.path.join("D:/cfg", "copilot")


def test_resolve_jobs_dir_explicit_wins():
    assert resolve_jobs_dir(_cfg(jobs_dir="E:/jobs")) == "E:/jobs"


def test_persist_content_writes_file_and_returns_job(tmp_path):
    content = {"stage_name": "", "opers": [{"name": "山"}]}
    job = persist_content(
        str(tmp_path), 1001, content,
        stage_display="1-7", level_id="obt/main/level_main_01-07",
    )
    assert job.filename.endswith("1001.json")
    assert job.job_id == 1001
    assert job.stage_display == "1-7"
    assert job.level_id == "obt/main/level_main_01-07"
    assert job.stage_name == "obt/main/level_main_01-07"  # 缺省 = level_id
    assert job.is_raid is False
    # 落盘内容 == 传入全文（供 maa 引用本地文件，不二次下载）。
    assert json.load(open(job.filename, encoding="utf-8")) == content


def test_persist_from_result_carries_context(tmp_path):
    result = CatalogResult(
        stage_display="1-7",
        level_id="obt/main/level_main_01-07",
        collision=[],
        total_fetched=1,
        candidates=[],
        contents={1001: {"opers": [{"name": "山"}]}},
    )
    job = persist_from_result(result, 1001, str(tmp_path))
    # 执行后仍需的上下文全保留（#5/#6 用）。
    assert job.job_id == 1001
    assert job.stage_display == "1-7"
    assert job.level_id == "obt/main/level_main_01-07"
    # content.stage_name 可能为空（§十一 修正1）→ 用查询 level_id 兜底。
    assert job.stage_name == "obt/main/level_main_01-07"
    assert json.load(open(job.filename, encoding="utf-8")) == {"opers": [{"name": "山"}]}


def test_persist_from_result_stage_name_override(tmp_path):
    result = CatalogResult(
        stage_display="1-7", level_id="obt/main/level_main_01-07",
        collision=[], total_fetched=1, candidates=[],
        contents={1001: {"opers": []}},
    )
    job = persist_from_result(result, 1001, str(tmp_path), stage_name="main_01-07")
    assert job.stage_name == "main_01-07"


def test_persist_from_result_unknown_id_raises(tmp_path):
    result = CatalogResult(
        stage_display="1-7", level_id="x", collision=[], total_fetched=0,
        candidates=[], contents={},
    )
    with pytest.raises(KeyError):
        persist_from_result(result, 9999, str(tmp_path))
