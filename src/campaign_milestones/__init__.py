"""治理阶段进度数仓。

从本地合成的阶段计划、目标范围与执行回执建立仅追加的 SQLite 数仓，
支持按任意历史记录时点（as-of）复核阶段口径与进度，并解释目标、执行、
复核与未完成事项之间的关系。
"""
from __future__ import annotations

from .ingest import ingest_batch
from .queries import (
    explain_item,
    item_status,
    list_stages,
    stage_conclusion,
    unverified_items,
)
from .schema import connect, init_db

__version__ = "0.1.0"

__all__ = [
    "connect",
    "init_db",
    "ingest_batch",
    "stage_conclusion",
    "explain_item",
    "item_status",
    "list_stages",
    "unverified_items",
]
