"""按记录时间（as-of）解析阶段口径、事项状态与阶段结论。

两个时间轴
----------

- **记录时间** ``ingested_at``：记录进入数仓的时间。as-of 查询只看
  ``ingested_at <= T`` 的记录，从而重放"当时数仓知道什么"。
- **业务时间**（``valid_from`` / ``due_at`` / ``started_at`` / ``reported_at``
  / ``recorded_at`` / ``confirmed_at``）：记录自带的业务时点，用于判断
  迟报、截止后调整、启动早于确认等情形。

解析规则
--------

1. 阶段有效修订 = as-of 时点已入库的、revision 最大的阶段记录；
   若该记录入库晚于上一修订的截止时间，标记为"截止后调整"。
2. 目标按 objective_id 取最新版本；只有 confirmed / changed 视为经确认
   口径进入分母，draft / withdrawn 不进入分母。目标的 stage_revision 必须
   等于阶段有效修订，否则视为被替换的历史口径——其进度保留在调节项中
   备查，不并入新分母。
3. 事项范围按 (objective_id, work_item_id) 取最新范围记录，included 才在
   范围内；excluded 的事项及其历史回执进入调节项，不静默并入也不丢弃。
4. 回执按 execution_id 取最新版本（补报、复核结论都以新版本到达）。
   同一事项多次交接时，以 started_at 最新（并列取 reported_at、
   execution_id）的执行单为当前持有，其复核状态决定事项状态。
5. 事项状态：verified / reported_unverified / returned / paused /
   no_evidence / out_of_scope。范围内但无任何回执的事项一律记为
   no_evidence，**绝不**用缺失记录推断工作已完成。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from .ingest import CONFIRMED_OBJECTIVE_STATES
from .timeutil import format_instant, normalize_instant

# 事项状态取值
STATUS_VERIFIED = "verified"
STATUS_REPORTED_UNVERIFIED = "reported_unverified"
STATUS_RETURNED = "returned"
STATUS_PAUSED = "paused"
STATUS_NO_EVIDENCE = "no_evidence"
STATUS_OUT_OF_SCOPE = "out_of_scope"

#: 复核结论到事项状态的映射。late 是"迟报"标记，复核上仍属已报未核实。
_REVIEW_TO_STATUS = {
    "accepted": STATUS_VERIFIED,
    "submitted": STATUS_REPORTED_UNVERIFIED,
    "late": STATUS_REPORTED_UNVERIFIED,
    "returned": STATUS_RETURNED,
    "paused": STATUS_PAUSED,
}

STATUS_REASONS = {
    STATUS_VERIFIED: "当前回执已验收",
    STATUS_REPORTED_UNVERIFIED: "已报回执，尚未复核",
    STATUS_RETURNED: "复核退回，待重新提交",
    STATUS_PAUSED: "条件变化，工作暂停",
    STATUS_NO_EVIDENCE: "范围内但无任何回执，不能推断完成",
    STATUS_OUT_OF_SCOPE: "已移出当前范围",
}


def _normalize_as_of(as_of: str | datetime | None) -> str | None:
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return format_instant(as_of)
    return normalize_instant(as_of)


# ---------------------------------------------------------------------------
# 视图对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageRevisionView:
    record_id: str
    stage_id: str
    revision: int
    valid_from: str
    due_at: str
    scope_ref: str
    stage_state: str
    ingested_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "StageRevisionView":
        return cls(
            record_id=row["record_id"],
            stage_id=row["stage_id"],
            revision=row["revision"],
            valid_from=row["valid_from"],
            due_at=row["due_at"],
            scope_ref=row["scope_ref"],
            stage_state=row["stage_state"],
            ingested_at=row["ingested_at"],
        )


@dataclass(frozen=True)
class ObjectiveView:
    record_id: str
    objective_id: str
    stage_id: str
    stage_revision: int
    measure_code: str
    expected_count: int
    confirmed_at: str | None
    objective_state: str
    ingested_at: str

    @property
    def is_confirmed(self) -> bool:
        return self.objective_state in CONFIRMED_OBJECTIVE_STATES

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ObjectiveView":
        return cls(
            record_id=row["record_id"],
            objective_id=row["objective_id"],
            stage_id=row["stage_id"],
            stage_revision=row["stage_revision"],
            measure_code=row["measure_code"],
            expected_count=row["expected_count"],
            confirmed_at=row["confirmed_at"],
            objective_state=row["objective_state"],
            ingested_at=row["ingested_at"],
        )


@dataclass(frozen=True)
class ScopeView:
    record_id: str
    objective_id: str
    work_item_id: str
    scope_state: str
    recorded_at: str
    stage_revision: int
    ingested_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ScopeView":
        return cls(
            record_id=row["record_id"],
            objective_id=row["objective_id"],
            work_item_id=row["work_item_id"],
            scope_state=row["scope_state"],
            recorded_at=row["recorded_at"],
            stage_revision=row["stage_revision"],
            ingested_at=row["ingested_at"],
        )


@dataclass(frozen=True)
class ExecutionView:
    record_id: str
    execution_id: str
    objective_id: str
    work_item_id: str
    team_id: str
    started_at: str
    reported_at: str
    reported_count: int
    review_state: str
    ingested_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ExecutionView":
        return cls(
            record_id=row["record_id"],
            execution_id=row["execution_id"],
            objective_id=row["objective_id"],
            work_item_id=row["work_item_id"],
            team_id=row["team_id"],
            started_at=row["started_at"],
            reported_at=row["reported_at"],
            reported_count=row["reported_count"],
            review_state=row["review_state"],
            ingested_at=row["ingested_at"],
        )


@dataclass(frozen=True)
class ItemStatusView:
    """一个 (目标, 事项) 在 as-of 时点的状态。"""

    objective_id: str
    work_item_id: str
    status: str
    scope_state: str | None
    governing: ExecutionView | None
    versions: tuple[ExecutionView, ...]
    any_accepted: bool
    late: bool
    reason: str
    warnings: tuple[str, ...] = ()


@dataclass
class ObjectiveProgress:
    """一个目标版本下的进度分解。"""

    objective: ObjectiveView
    caliber: str  # "current" 当前口径 | "superseded" 被替换的历史口径
    items: list[ItemStatusView] = field(default_factory=list)
    removed_items: list[ItemStatusView] = field(default_factory=list)
    orphan_receipts: list[ExecutionView] = field(default_factory=list)

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def verified_reported_count(self) -> int:
        """已核验事项的当前持有回执回报量之和（同一事项多次交接只计一次）。"""
        return sum(
            item.governing.reported_count
            for item in self.items
            if item.status == STATUS_VERIFIED and item.governing is not None
        )


@dataclass
class StageConclusion:
    """阶段结论复核结果：口径、进度分解、调节项与未核验清单。"""

    stage_id: str
    as_of: str | None
    exists: bool
    revision: StageRevisionView | None = None
    previous_due_at: str | None = None
    post_deadline_adjustment: bool = False
    confirmed_objectives: list[ObjectiveProgress] = field(default_factory=list)
    pending_objectives: list[ObjectiveView] = field(default_factory=list)
    superseded_objectives: list[ObjectiveProgress] = field(default_factory=list)
    unverified_items: list[ItemStatusView] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ItemObjectiveLine:
    objective: ObjectiveView
    status: ItemStatusView
    caliber: str


@dataclass
class ItemExplanation:
    """一个工作事项的完整说明：范围变迁、回执时间线、当前状态。"""

    work_item_id: str
    as_of: str | None
    scope_records: list[ScopeView] = field(default_factory=list)
    execution_records: list[ExecutionView] = field(default_factory=list)
    lines: list[ItemObjectiveLine] = field(default_factory=list)
    handoff_count: int = 0
    teams: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def _known_rows(
    conn: sqlite3.Connection, table: str, as_of: str | None, where: str, params: tuple
) -> list[sqlite3.Row]:
    sql = f"SELECT * FROM {table} WHERE {where}"
    args: list = list(params)
    if as_of is not None:
        sql += " AND ingested_at <= ?"
        args.append(as_of)
    return list(conn.execute(sql, args))


def stage_revisions(
    conn: sqlite3.Connection, stage_id: str, as_of: str | datetime | None = None
) -> list[StageRevisionView]:
    """阶段在 as-of 时点已知的全部修订，按 (revision, 入库时间) 升序。"""
    bound = _normalize_as_of(as_of)
    rows = _known_rows(conn, "raw_stage_record", bound, "stage_id = ?", (stage_id,))
    views = [StageRevisionView.from_row(row) for row in rows]
    views.sort(key=lambda v: (v.revision, v.ingested_at, v.record_id))
    return views


def effective_stage_revision(
    conn: sqlite3.Connection, stage_id: str, as_of: str | datetime | None = None
) -> StageRevisionView | None:
    """as-of 时点的有效修订：已入库记录中 revision 最大者。"""
    revisions = stage_revisions(conn, stage_id, as_of)
    return revisions[-1] if revisions else None


def list_stages(
    conn: sqlite3.Connection, as_of: str | datetime | None = None
) -> list[StageRevisionView]:
    """as-of 时点所有已知阶段及其有效修订。"""
    bound = _normalize_as_of(as_of)
    rows = _known_rows(conn, "raw_stage_record", bound, "1 = 1", ())
    by_stage: dict[str, list[StageRevisionView]] = {}
    for row in rows:
        by_stage.setdefault(row["stage_id"], []).append(StageRevisionView.from_row(row))
    effective = []
    for revisions in by_stage.values():
        revisions.sort(key=lambda v: (v.revision, v.ingested_at, v.record_id))
        effective.append(revisions[-1])
    effective.sort(key=lambda v: v.stage_id)
    return effective


def objective_versions(
    conn: sqlite3.Connection, stage_id: str, as_of: str | datetime | None = None
) -> list[ObjectiveView]:
    """阶段下各目标的最新版本（按 objective_id 去重）。"""
    bound = _normalize_as_of(as_of)
    rows = _known_rows(conn, "raw_objective_record", bound, "stage_id = ?", (stage_id,))
    latest: dict[str, ObjectiveView] = {}
    for row in rows:
        view = ObjectiveView.from_row(row)
        key = view.objective_id
        current = latest.get(key)
        if current is None or (view.ingested_at, view.record_id) >= (
            current.ingested_at,
            current.record_id,
        ):
            latest[key] = view
    return sorted(latest.values(), key=lambda v: v.objective_id)


def objective_version(
    conn: sqlite3.Connection, objective_id: str, as_of: str | datetime | None = None
) -> ObjectiveView | None:
    """单个目标的最新版本。"""
    bound = _normalize_as_of(as_of)
    rows = _known_rows(
        conn, "raw_objective_record", bound, "objective_id = ?", (objective_id,)
    )
    if not rows:
        return None
    views = [ObjectiveView.from_row(row) for row in rows]
    views.sort(key=lambda v: (v.ingested_at, v.record_id))
    return views[-1]


def effective_scope(
    conn: sqlite3.Connection, objective_id: str, as_of: str | datetime | None = None
) -> dict[str, ScopeView]:
    """目标在 as-of 时点的有效范围：每个事项取最新范围记录。"""
    bound = _normalize_as_of(as_of)
    rows = _known_rows(
        conn, "raw_scope_record", bound, "objective_id = ?", (objective_id,)
    )
    latest: dict[str, ScopeView] = {}
    for row in rows:
        view = ScopeView.from_row(row)
        key = view.work_item_id
        current = latest.get(key)
        if current is None or (view.recorded_at, view.ingested_at, view.record_id) >= (
            current.recorded_at,
            current.ingested_at,
            current.record_id,
        ):
            latest[key] = view
    return latest


def execution_versions(
    conn: sqlite3.Connection,
    *,
    objective_id: str | None = None,
    work_item_id: str | None = None,
    as_of: str | datetime | None = None,
) -> list[ExecutionView]:
    """回执的全部版本（补报、复核结论各占一行）。"""
    bound = _normalize_as_of(as_of)
    clauses, params = [], []
    if objective_id is not None:
        clauses.append("objective_id = ?")
        params.append(objective_id)
    if work_item_id is not None:
        clauses.append("work_item_id = ?")
        params.append(work_item_id)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    rows = _known_rows(conn, "raw_execution_record", bound, where, tuple(params))
    views = [ExecutionView.from_row(row) for row in rows]
    views.sort(key=lambda v: (v.started_at, v.reported_at, v.ingested_at, v.record_id))
    return views


def latest_executions(
    conn: sqlite3.Connection,
    *,
    objective_id: str | None = None,
    work_item_id: str | None = None,
    as_of: str | datetime | None = None,
) -> dict[str, ExecutionView]:
    """每个 execution_id 的最新版本。"""
    latest: dict[str, ExecutionView] = {}
    for view in execution_versions(
        conn, objective_id=objective_id, work_item_id=work_item_id, as_of=as_of
    ):
        current = latest.get(view.execution_id)
        if current is None or (view.ingested_at, view.record_id) >= (
            current.ingested_at,
            current.record_id,
        ):
            latest[view.execution_id] = view
    return latest


def _due_at_for(
    conn: sqlite3.Connection, objective: ObjectiveView, as_of: str | None
) -> str | None:
    """目标所属阶段修订的截止时间（用于迟报判断）。"""
    rows = _known_rows(
        conn,
        "raw_stage_record",
        as_of,
        "stage_id = ? AND revision = ?",
        (objective.stage_id, objective.stage_revision),
    )
    if not rows:
        return None
    views = [StageRevisionView.from_row(row) for row in rows]
    views.sort(key=lambda v: (v.ingested_at, v.record_id))
    return views[-1].due_at


def _first_confirmed_at(
    conn: sqlite3.Connection, objective_id: str, as_of: str | None
) -> str | None:
    """目标首次被确认的时间（跨所有版本取最早）。"""
    rows = _known_rows(
        conn,
        "raw_objective_record",
        as_of,
        "objective_id = ? AND confirmed_at IS NOT NULL",
        (objective_id,),
    )
    if not rows:
        return None
    return min(row["confirmed_at"] for row in rows)


# ---------------------------------------------------------------------------
# 事项状态
# ---------------------------------------------------------------------------


def _build_item_status(
    objective: ObjectiveView,
    work_item_id: str,
    scope_state: str | None,
    versions: list[ExecutionView],
    due_at: str | None,
    first_confirmed_at: str | None,
) -> ItemStatusView:
    governing: ExecutionView | None = None
    if scope_state != "included":
        status = STATUS_OUT_OF_SCOPE
    elif not versions:
        status = STATUS_NO_EVIDENCE
    else:
        # 多次交接时以启动最新者为当前持有；并列时取回报时间、执行单号。
        governing = max(
            versions, key=lambda v: (v.started_at, v.reported_at, v.execution_id)
        )
        status = _REVIEW_TO_STATUS[governing.review_state]
    if governing is None and versions:
        governing = max(
            versions, key=lambda v: (v.started_at, v.reported_at, v.execution_id)
        )

    late = bool(
        governing is not None and due_at is not None and governing.reported_at > due_at
    )
    reason = STATUS_REASONS[status]
    if late and status in (STATUS_REPORTED_UNVERIFIED, STATUS_VERIFIED):
        reason += "；迟报：回报晚于阶段截止"

    warnings: list[str] = []
    if governing is not None and governing.reported_at < governing.started_at:
        warnings.append("回执回报时间早于启动时间")
    if (
        governing is not None
        and first_confirmed_at is not None
        and governing.started_at < first_confirmed_at
    ):
        warnings.append("事项启动早于目标首次确认时间")

    return ItemStatusView(
        objective_id=objective.objective_id,
        work_item_id=work_item_id,
        status=status,
        scope_state=scope_state,
        governing=governing,
        versions=tuple(versions),
        any_accepted=any(v.review_state == "accepted" for v in versions),
        late=late,
        reason=reason,
        warnings=tuple(warnings),
    )


def item_status(
    conn: sqlite3.Connection,
    objective_id: str,
    work_item_id: str,
    as_of: str | datetime | None = None,
) -> ItemStatusView | None:
    """单个 (目标, 事项) 在 as-of 时点的状态；目标未知时返回 None。"""
    bound = _normalize_as_of(as_of)
    objective = objective_version(conn, objective_id, bound)
    if objective is None:
        return None
    scope = effective_scope(conn, objective_id, bound)
    scope_view = scope.get(work_item_id)
    versions = [
        view
        for view in latest_executions(
            conn, objective_id=objective_id, work_item_id=work_item_id, as_of=bound
        ).values()
    ]
    due_at = _due_at_for(conn, objective, bound)
    return _build_item_status(
        objective,
        work_item_id,
        scope_view.scope_state if scope_view else None,
        versions,
        due_at,
        _first_confirmed_at(conn, objective_id, bound),
    )


# ---------------------------------------------------------------------------
# 阶段结论
# ---------------------------------------------------------------------------


def _objective_progress(
    conn: sqlite3.Connection,
    objective: ObjectiveView,
    caliber: str,
    as_of: str | None,
) -> ObjectiveProgress:
    scope = effective_scope(conn, objective.objective_id, as_of)
    executions = latest_executions(conn, objective_id=objective.objective_id, as_of=as_of)
    by_item: dict[str, list[ExecutionView]] = {}
    for view in executions.values():
        by_item.setdefault(view.work_item_id, []).append(view)
    due_at = _due_at_for(conn, objective, as_of)
    first_confirmed_at = _first_confirmed_at(conn, objective.objective_id, as_of)

    progress = ObjectiveProgress(objective=objective, caliber=caliber)
    for work_item_id, scope_view in sorted(scope.items()):
        status = _build_item_status(
            objective,
            work_item_id,
            scope_view.scope_state,
            by_item.get(work_item_id, []),
            due_at,
            first_confirmed_at,
        )
        if scope_view.scope_state == "included":
            progress.items.append(status)
        else:
            progress.removed_items.append(status)
    # 范围外证据：有回执但该事项从未出现在此目标的任何范围记录中。
    for work_item_id, versions in sorted(by_item.items()):
        if work_item_id not in scope:
            progress.orphan_receipts.extend(versions)
    return progress


def stage_conclusion(
    conn: sqlite3.Connection, stage_id: str, as_of: str | datetime | None = None
) -> StageConclusion:
    """复核阶段结论：有效口径来自哪些目标、哪些实际事件、哪些事项未核验。"""
    bound = _normalize_as_of(as_of)
    revisions = stage_revisions(conn, stage_id, bound)
    if not revisions:
        return StageConclusion(stage_id=stage_id, as_of=bound, exists=False)

    effective = revisions[-1]
    previous = next(
        (rev for rev in reversed(revisions) if rev.revision < effective.revision), None
    )
    # 截止后调整按业务时间判断：新口径的生效时间晚于上一修订的截止时间。
    # （入库时间只说明数仓何时得知，批量补录不应被误判为截止后调整。）
    post_deadline = bool(previous and effective.valid_from > previous.due_at)

    conclusion = StageConclusion(
        stage_id=stage_id,
        as_of=bound,
        exists=True,
        revision=effective,
        previous_due_at=previous.due_at if previous else None,
        post_deadline_adjustment=post_deadline,
    )
    if post_deadline:
        conclusion.notes.append(
            f"修订 {effective.revision} 生效于 {effective.valid_from}，"
            f"晚于上一修订截止 {previous.due_at}，属截止后调整"
        )

    for objective in objective_versions(conn, stage_id, bound):
        if objective.stage_revision != effective.revision:
            conclusion.superseded_objectives.append(
                _objective_progress(conn, objective, "superseded", bound)
            )
        elif objective.is_confirmed:
            conclusion.confirmed_objectives.append(
                _objective_progress(conn, objective, "current", bound)
            )
        else:
            conclusion.pending_objectives.append(objective)

    if conclusion.pending_objectives:
        ids = "、".join(obj.objective_id for obj in conclusion.pending_objectives)
        conclusion.notes.append(f"目标 {ids} 尚未确认，不进入当前分母")

    conclusion.unverified_items = [
        item
        for progress in conclusion.confirmed_objectives
        for item in progress.items
        if item.status != STATUS_VERIFIED
    ]
    return conclusion


def unverified_items(
    conn: sqlite3.Connection,
    stage_id: str | None = None,
    as_of: str | datetime | None = None,
) -> list[tuple[str, ItemStatusView]]:
    """未核验事项清单：范围内但证据不足（未报、待复核、退回、暂停）。"""
    bound = _normalize_as_of(as_of)
    stage_ids = [stage_id] if stage_id else [s.stage_id for s in list_stages(conn, bound)]
    result: list[tuple[str, ItemStatusView]] = []
    for sid in stage_ids:
        conclusion = stage_conclusion(conn, sid, bound)
        for item in conclusion.unverified_items:
            result.append((sid, item))
    return result


def explain_item(
    conn: sqlite3.Connection, work_item_id: str, as_of: str | datetime | None = None
) -> ItemExplanation:
    """说明一个事项：范围变迁、全部回执版本、交接次数与当前状态。"""
    bound = _normalize_as_of(as_of)
    scope_rows = _known_rows(
        conn, "raw_scope_record", bound, "work_item_id = ?", (work_item_id,)
    )
    scope_records = [ScopeView.from_row(row) for row in scope_rows]
    scope_records.sort(key=lambda v: (v.recorded_at, v.ingested_at, v.record_id))

    execution_records = execution_versions(conn, work_item_id=work_item_id, as_of=bound)

    explanation = ItemExplanation(
        work_item_id=work_item_id,
        as_of=bound,
        scope_records=scope_records,
        execution_records=execution_records,
    )
    explanation.handoff_count = len({view.execution_id for view in execution_records})
    seen_teams: dict[str, None] = {}
    for view in execution_records:
        seen_teams.setdefault(view.team_id)
    explanation.teams = list(seen_teams)

    objective_ids = sorted(
        {view.objective_id for view in scope_records}
        | {view.objective_id for view in execution_records}
    )
    for objective_id in objective_ids:
        objective = objective_version(conn, objective_id, bound)
        if objective is None:
            continue
        status = item_status(conn, objective_id, work_item_id, bound)
        if status is None:
            continue
        effective = effective_stage_revision(conn, objective.stage_id, bound)
        caliber = (
            "current"
            if effective is not None and objective.stage_revision == effective.revision
            else "superseded"
        )
        explanation.lines.append(
            ItemObjectiveLine(objective=objective, status=status, caliber=caliber)
        )
    return explanation
