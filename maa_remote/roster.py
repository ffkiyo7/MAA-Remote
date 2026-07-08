"""干员练度数据（roster）。

练度数据源分层（设计文档 §三）：
  - OperBox（MAA 干员识别）为基线：own/elite/level/potential，**不含**技能等级/专精/模组；
  - 森空岛（S3，可选增强）跑通后补技能/专精/模组盲区。

本模块只承载练度数据的**表示与查询**（硬过滤/软打分要用）。真实数据的抓取与解析：
  - OperBox 解析规则待真实回调样本锁定（缺口 #10，`scripts/spike_copilot.py operbox`）；
  - skland client 待 token live 定案（缺口 #9 / S3）。
在样本到位前，`Roster.load` 只负责读 roster.json 缓存的既定形状，写入方后补。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# 多字规范名的皮肤（凛御银灰/纯烬艾雅法拉/斩业星熊 …）由 Roster 的后缀归一自动覆盖；
# 这里只补单字规范名等后缀启发式无法安全归一的特例。可按需扩充（MAA 自身识别会归一）。
SKIN_ALIASES = {
    "历阵锐枪芬": "芬",
}


@dataclass
class Roster:
    """干员练度数据。真实场景由 OperBox/Skland 填充。

    注意：真实 OperBox 回调不含 skill_level（§2.4）→ 缺省即"无数据"，
    匹配层据此走风险标注而非淘汰。
    """

    owned: dict = field(default_factory=dict)  # {规范名: {elite, level, skill_level?, module?}}
    source: str = ""      # "operbox" | "skland" | ""（mock/未知）
    fetched_at: str = ""  # ISO8601，练度缓存时间；匹配时用于"练度是 X 天前的"提示

    @classmethod
    def mock(cls) -> "Roster":
        """一份中等偏上的假 Box（6★ 精二常见干员）。

        刻意不含 skill_level —— 与真实 OperBox 一致（§2.4），用于暴露技能盲区路径。
        """
        return cls(
            owned={
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
            },
            source="mock",
        )

    @classmethod
    def load(cls, path: str) -> "Roster":
        """从 roster.json 缓存加载。形状：{source, fetched_at, owned:{名:{elite,level,...}}}。"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # 兼容裸 owned dict（如手写的 roster 文件）与带元数据的完整形状。
        if "owned" in data and isinstance(data["owned"], dict):
            return cls(
                owned=data["owned"],
                source=data.get("source", ""),
                fetched_at=data.get("fetched_at", ""),
            )
        return cls(owned=data, source="", fetched_at="")

    def is_empty(self) -> bool:
        return not self.owned

    def get(self, name: str) -> Optional[dict]:
        """按干员名取练度，自动归一皮肤别名。"""
        if name in self.owned:
            return self.owned[name]
        canon = self._canonical(name)
        if canon is not None:
            return self.owned.get(canon)
        return None

    def _canonical(self, name: str) -> Optional[str]:
        """把皮肤名归一到自有的规范名，归一不到返回 None。"""
        alias = SKIN_ALIASES.get(name)
        if alias and alias in self.owned:
            return alias
        # 皮肤名 = 装饰前缀 + 规范名，规范名总在词尾。取最长的自有后缀（>=2 字避免误配）。
        best = None
        for k in self.owned:
            if len(k) >= 2 and name != k and name.endswith(k):
                if best is None or len(k) > len(best):
                    best = k
        return best
