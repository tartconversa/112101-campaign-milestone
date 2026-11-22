"""把来源 CSV 以批次形式追加进原始层。

每个批次携带明确的 ``ingested_at``（记录时间），它是 as-of 历史查询的
时间轴。摄取只做格式与取值校验（表头、非空、枚举、时间格式、record_id
全局唯一），不做跨表引用检查——补报、退回重报、范围外证据等业务形态
本来就允许"回执先于范围到达"，这类差异留给查询层解释而不是在入口拒绝。
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .timeutil import normalize_instant, parse_optional

# 各来源允许的枚举取值。新增取值需先在此登记，避免脏数据进入原始层。
STAGE_STATES = frozenset({"planned", "active", "amended", "closed"})
OBJECTIVE_STATES = frozenset({"draft", "confirmed", "changed", "withdrawn"})
SCOPE_STATES = frozenset({"included", "excluded"})
REVIEW_STATES = frozenset({"submitted", "late", "accepted", "returned", "paused"})

#: 目标状态中表示"经确认、可进入阶段分母"的集合。
#: confirmed = 首次确认；changed = 确认后调整（仍属有效口径）。
CONFIRMED_OBJECTIVE_STATES = frozenset({"confirmed", "changed"})


@dataclass(frozen=True)
class SourceSpec:
    """一个来源文件的摄取规则。"""

    key: str
    filename: str
    table: str
    rules: tuple[tuple[str, tuple], ...]

    @property
    def columns(self) -> list[str]:
        return [name for name, _ in self.rules]


# 列规则形式：("text",) 非空文本；("int",) 非负整数；("instant",) 必填时间；
# ("instant?",) 可空时间；("enum", 集合) 枚举。
_SPECS = (
    SourceSpec(
        key="stages",
        filename="stages.csv",
        table="raw_stage_record",
        rules=(
            ("record_id", ("text",)),
            ("stage_id", ("text",)),
            ("revision", ("int",)),
            ("valid_from", ("instant",)),
            ("due_at", ("instant",)),
            ("scope_ref", ("text",)),
            ("stage_state", ("enum", STAGE_STATES)),
        ),
    ),
    SourceSpec(
        key="objectives",
        filename="objectives.csv",
        table="raw_objective_record",
        rules=(
            ("record_id", ("text",)),
            ("objective_id", ("text",)),
            ("stage_id", ("text",)),
            ("stage_revision", ("int",)),
            ("measure_code", ("text",)),
            ("expected_count", ("int",)),
            ("confirmed_at", ("instant?",)),
            ("objective_state", ("enum", OBJECTIVE_STATES)),
        ),
    ),
    SourceSpec(
        key="scoped_items",
        filename="scoped_items.csv",
        table="raw_scope_record",
        rules=(
            ("record_id", ("text",)),
            ("scope_record_id", ("text",)),
            ("objective_id", ("text",)),
            ("stage_revision", ("int",)),
            ("work_item_id", ("text",)),
            ("scope_state", ("enum", SCOPE_STATES)),
            ("recorded_at", ("instant",)),
        ),
    ),
    SourceSpec(
        key="execution",
        filename="execution.csv",
        table="raw_execution_record",
        rules=(
            ("record_id", ("text",)),
            ("execution_id", ("text",)),
            ("objective_id", ("text",)),
            ("work_item_id", ("text",)),
            ("team_id", ("text",)),
            ("started_at", ("instant",)),
            ("reported_at", ("instant",)),
            ("reported_count", ("int",)),
            ("review_state", ("enum", REVIEW_STATES)),
        ),
    ),
)

SPECS: dict[str, SourceSpec] = {spec.key: spec for spec in _SPECS}


def _coerce_value(rule: tuple, raw_value: str | None, *, where: str):
    kind = rule[0]
    raw = (raw_value or "").strip()
    if kind == "text":
        if not raw:
            raise ValueError(f"{where}: 字段不能为空")
        return raw
    if kind == "int":
        try:
            number = int(raw)
        except ValueError as exc:
            raise ValueError(f"{where}: 不是合法整数 {raw!r}") from exc
        if number < 0:
            raise ValueError(f"{where}: 不允许负数 {raw!r}")
        return number
    if kind == "instant":
        if not raw:
            raise ValueError(f"{where}: 时间字段不能为空")
        return normalize_instant(raw)
    if kind == "instant?":
        return parse_optional(raw)
    if kind == "enum":
        allowed = rule[1]
        if raw not in allowed:
            raise ValueError(f"{where}: 非法取值 {raw!r}，允许 {sorted(allowed)}")
        return raw
    raise AssertionError(f"未知列规则: {rule!r}")


def _read_source(spec: SourceSpec, path: Path) -> list[dict]:
    """读取并校验一个来源文件，返回规范化后的行。"""
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        expected = set(spec.columns)
        actual = set(reader.fieldnames or ())
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                f"{path.name}: 表头与预期不符，缺少 {missing}，多余 {extra}"
            )
        rows: list[dict] = []
        seen: set[str] = set()
        for line_no, raw_row in enumerate(reader, start=2):
            record_id = (raw_row.get("record_id") or "").strip() or f"第{line_no}行"
            where = f"{path.name} 记录 {record_id}"
            row = {
                column: _coerce_value(rule, raw_row.get(column), where=f"{where} 字段 {column}")
                for column, rule in spec.rules
            }
            if row["record_id"] in seen:
                raise ValueError(f"{path.name}: record_id {row['record_id']!r} 在文件内重复")
            seen.add(row["record_id"])
            rows.append(row)
    if not rows:
        raise ValueError(f"{path.name}: 文件没有任何数据行")
    _post_check(spec, rows, path.name)
    return rows


def _post_check(spec: SourceSpec, rows: list[dict], filename: str) -> None:
    """跨字段一致性校验。"""
    if spec.key == "objectives":
        for row in rows:
            if row["objective_state"] in CONFIRMED_OBJECTIVE_STATES and not row["confirmed_at"]:
                raise ValueError(
                    f"{filename} 记录 {row['record_id']}: "
                    f"状态为 {row['objective_state']} 时 confirmed_at 不能为空"
                )


def _check_not_already_loaded(conn: sqlite3.Connection, spec: SourceSpec, rows: list[dict]) -> None:
    """record_id 全局唯一：同一记录重复入库会破坏 as-of 重放，提前报错。"""
    ids = [row["record_id"] for row in rows]
    placeholders = ", ".join("?" for _ in ids)
    found = conn.execute(
        f"SELECT record_id FROM {spec.table} WHERE record_id IN ({placeholders})", ids
    ).fetchall()
    if found:
        duplicates = sorted(row["record_id"] for row in found)
        raise ValueError(f"{spec.filename}: 以下 record_id 已入库，不能重复追加 {duplicates}")


def _insert_rows(conn: sqlite3.Connection, spec: SourceSpec, batch_id: int, ingested_at: str, rows: list[dict]) -> None:
    columns = ["batch_id", *spec.columns, "ingested_at"]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {spec.table} ({', '.join(columns)}) VALUES ({placeholders})"
    conn.executemany(
        sql,
        ([batch_id, *[row[column] for column in spec.columns], ingested_at] for row in rows),
    )


def ingest_batch(
    conn: sqlite3.Connection,
    *,
    label: str,
    ingested_at: str,
    directory: str | Path | None = None,
    files: Mapping[str, str | Path] | None = None,
    note: str = "",
) -> int:
    """把一个批次追加进原始层，返回 batch_id。

    ``directory`` 指向包含来源 CSV 的目录（按约定文件名取存在的文件）；
    或用 ``files`` 显式给出 ``{来源键: 路径}``。两者至少提供一个文件，
    整个批次在单个事务中写入，失败时整体回滚。
    """
    moment = normalize_instant(ingested_at)
    provided: list[tuple[SourceSpec, Path]] = []
    if directory is not None:
        base = Path(directory)
        for spec in SPECS.values():
            candidate = base / spec.filename
            if candidate.is_file():
                provided.append((spec, candidate))
    if files:
        for key, path in files.items():
            if key not in SPECS:
                raise ValueError(f"未知来源键 {key!r}，允许 {sorted(SPECS)}")
            provided.append((SPECS[key], Path(path)))
    if not provided:
        raise ValueError("批次未提供任何来源文件")

    parsed = [(spec, path, _read_source(spec, path)) for spec, path in provided]

    with conn:  # 单事务：批次要么完整入库，要么整体回滚
        cursor = conn.execute(
            "INSERT INTO ingest_batch (batch_label, ingested_at, note) VALUES (?, ?, ?)",
            (label, moment, note),
        )
        batch_id = int(cursor.lastrowid)
        for spec, path, rows in parsed:
            _check_not_already_loaded(conn, spec, rows)
            _insert_rows(conn, spec, batch_id, moment, rows)
    return batch_id
