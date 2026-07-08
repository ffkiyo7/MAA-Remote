"""作业落盘 + 运行上下文（catalog ↔ executor 的接缝，设计文档 §五 line 121）。

匹配阶段查询结果已含作业全文（`CatalogResult.contents`），选中候选后由本模块落盘到
`jobs_dir`，产出 `CopilotJob`（本地 filename + stage_name），供 TaskPlan/executor 引用——
**不二次下载**。stage_name 用查询到的 level_id 兜底（content.stage_name 实测可能为空，§十一 修正1），
copilot_list 导航到底吃 level_id/stage_id/显示号属 S2 未决，故 stage_name 允许上层覆写。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from maa_remote.copilot_catalog import CatalogResult
from maa_remote.models import CopilotJob


def resolve_jobs_dir(cfg) -> str:
    """作业落盘目录：配置显式指定 > <config_dir>/copilot。"""
    return cfg.copilot.jobs_dir or os.path.join(cfg.maa.config_dir, "copilot")


def persist_content(
    jobs_dir: str,
    job_id: int,
    content: dict,
    *,
    stage_display: str = "",
    level_id: str = "",
    stage_name: Optional[str] = None,
    is_raid: bool = False,
) -> CopilotJob:
    """把一份作业全文落盘到 <jobs_dir>/<job_id>.json，返回引用本地文件的 CopilotJob。

    stage_name 缺省 = level_id（content.stage_name 实测可能为空，§十一 修正1）；
    job_id/stage_display/level_id 一并写进 CopilotJob，供 #5/#6 执行后仍能引用。
    """
    os.makedirs(jobs_dir, exist_ok=True)
    path = os.path.join(jobs_dir, f"{job_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    return CopilotJob(
        filename=path,
        job_id=job_id,
        stage_display=stage_display,
        level_id=level_id,
        stage_name=stage_name if stage_name is not None else level_id,
        is_raid=is_raid,
    )


def persist_from_result(
    result: CatalogResult,
    job_id: int,
    jobs_dir: str,
    *,
    stage_name: Optional[str] = None,
    is_raid: bool = False,
) -> CopilotJob:
    """从候选结果落盘选中的作业，带上 stage_display/level_id 上下文。

    stage_name 缺省用 result.level_id；S2 定案后可由上层覆写成 copilot_list 真正需要的形式。
    选中 id 不在候选结果中 → KeyError（防落盘一份没匹配过的作业）。
    """
    content = result.contents.get(job_id)
    if content is None:
        raise KeyError(f"作业 {job_id} 不在候选结果中，无法落盘")
    return persist_content(
        jobs_dir,
        job_id,
        content,
        stage_display=result.stage_display,
        level_id=result.level_id,
        stage_name=stage_name,
        is_raid=is_raid,
    )
