"""把查询结果渲染为管理者可读的中文文本报告。"""
from __future__ import annotations

from .queries import (
    ItemExplanation,
    ItemStatusView,
    ObjectiveProgress,
    StageConclusion,
    StageRevisionView,
    STATUS_NO_EVIDENCE,
    STATUS_OUT_OF_SCOPE,
    STATUS_PAUSED,
    STATUS_REPORTED_UNVERIFIED,
    STATUS_RETURNED,
    STATUS_VERIFIED,
)

STATUS_LABELS = {
    STATUS_VERIFIED: "已核验",
    STATUS_REPORTED_UNVERIFIED: "已报未核实",
    STATUS_RETURNED: "核实退回",
    STATUS_PAUSED: "已暂停",
    STATUS_NO_EVIDENCE: "无回执记录",
    STATUS_OUT_OF_SCOPE: "已移出范围",
}

_COUNT_ORDER = (
    STATUS_VERIFIED,
    STATUS_REPORTED_UNVERIFIED,
    STATUS_RETURNED,
    STATUS_PAUSED,
    STATUS_NO_EVIDENCE,
)


def _as_of_label(as_of: str | None) -> str:
    return as_of if as_of else "当前最新"


def _item_line(item: ItemStatusView, indent: str = "  - ") -> str:
    parts = [f"{indent}{item.work_item_id}：{STATUS_LABELS[item.status]}（{item.reason}）"]
    if item.governing is not None:
        gov = item.governing
        parts.append(
            f"{indent}  当前持有 {gov.team_id}/{gov.execution_id}，"
            f"启动 {gov.started_at}，报于 {gov.reported_at}，"
            f"回报量 {gov.reported_count}，复核状态 {gov.review_state}"
        )
    if item.any_accepted and item.status != STATUS_VERIFIED:
        parts.append(f"{indent}  注意：历史上存在已验收回执，但当前持有回执未验收")
    for warning in item.warnings:
        parts.append(f"{indent}  数据质量提示：{warning}")
    return "\n".join(parts)


def _objective_block(progress: ObjectiveProgress) -> list[str]:
    objective = progress.objective
    lines = [
        f"目标 {objective.objective_id}（度量 {objective.measure_code}，"
        f"期望 {objective.expected_count}，确认于 {objective.confirmed_at or '—'}，"
        f"状态 {objective.objective_state}，所属修订 {objective.stage_revision}）"
    ]
    counts = progress.status_counts()
    summary = "，".join(
        f"{STATUS_LABELS[status]} {counts.get(status, 0)}" for status in _COUNT_ORDER
    )
    lines.append(f"  范围内事项 {len(progress.items)}：{summary}")
    lines.append(
        f"  已核验回报量 {progress.verified_reported_count()} / 期望 {objective.expected_count}"
    )
    for item in progress.items:
        lines.append(_item_line(item))
    for item in progress.removed_items:
        evidence = "有历史回执" if item.versions else "无回执"
        lines.append(
            f"  - {item.work_item_id}：已移出范围（{evidence}，保留备查，不并入分母）"
        )
    for receipt in progress.orphan_receipts:
        lines.append(
            f"  - 范围外证据 {receipt.work_item_id}：{receipt.execution_id}/"
            f"{receipt.team_id} 报于 {receipt.reported_at}（{receipt.review_state}），"
            f"该事项从未纳入此目标范围"
        )
    return lines


def render_stage_list(revisions: list[StageRevisionView], as_of: str | None = None) -> str:
    lines = [f"阶段清单（记录时点：{_as_of_label(as_of)}）"]
    if not revisions:
        lines.append("  （该时点无任何阶段记录）")
    for rev in revisions:
        lines.append(
            f"  {rev.stage_id}：有效修订 {rev.revision}，生效 {rev.valid_from}，"
            f"截止 {rev.due_at}，范围 {rev.scope_ref}，状态 {rev.stage_state}"
        )
    return "\n".join(lines)


def render_stage_conclusion(conclusion: StageConclusion) -> str:
    lines = [
        f"阶段 {conclusion.stage_id} 结论复核（记录时点：{_as_of_label(conclusion.as_of)}）"
    ]
    if not conclusion.exists:
        lines.append("  该时点数仓中没有任何阶段记录，无法给出结论。")
        return "\n".join(lines)

    rev = conclusion.revision
    assert rev is not None
    lines.append(
        f"有效口径：修订 {rev.revision}（生效 {rev.valid_from}，截止 {rev.due_at}，"
        f"范围 {rev.scope_ref}，状态 {rev.stage_state}，记录于 {rev.ingested_at}）"
    )
    for note in conclusion.notes:
        lines.append(f"警示：{note}")

    lines.append("")
    if conclusion.confirmed_objectives:
        lines.append(f"经确认目标（{len(conclusion.confirmed_objectives)}，构成当前分母）：")
        for progress in conclusion.confirmed_objectives:
            lines.extend(_objective_block(progress))
    else:
        lines.append("经确认目标：无（当前口径下没有已确认目标，不能计算完成度）")

    if conclusion.pending_objectives:
        lines.append("")
        lines.append("未确认目标（不进入分母）：")
        for objective in conclusion.pending_objectives:
            lines.append(
                f"  - {objective.objective_id}（{objective.measure_code}，"
                f"期望 {objective.expected_count}，状态 {objective.objective_state}）"
            )

    if conclusion.superseded_objectives:
        lines.append("")
        lines.append("被替换的历史口径（进度保留备查，不并入当前分母）：")
        for progress in conclusion.superseded_objectives:
            lines.extend(_objective_block(progress))

    lines.append("")
    if conclusion.unverified_items:
        lines.append("未核验事项（证据不足，不能视为完成）：")
        for item in conclusion.unverified_items:
            lines.append(
                f"  - {item.objective_id}/{item.work_item_id}：{item.reason}"
            )
    else:
        lines.append("未核验事项：无（范围内事项均有已验收回执）")

    lines.append("")
    lines.append("说明：任务完成数不自动等价治理效果；缺失记录一律按未核验处理，"
                 "阶段是否达标由牵头团队依据以上证据判断。")
    return "\n".join(lines)


def render_item_explanation(explanation: ItemExplanation) -> str:
    lines = [
        f"事项 {explanation.work_item_id} 说明（记录时点：{_as_of_label(explanation.as_of)}）"
    ]
    if not explanation.scope_records and not explanation.execution_records:
        lines.append("  该时点数仓中没有任何与此事项相关的记录。")
        return "\n".join(lines)

    teams = "、".join(explanation.teams) if explanation.teams else "—"
    lines.append(f"交接次数 {explanation.handoff_count}，涉及团队：{teams}")

    lines.append("范围记录：")
    if explanation.scope_records:
        for record in explanation.scope_records:
            lines.append(
                f"  {record.recorded_at} {record.scope_state} → {record.objective_id}"
                f"（记录 {record.record_id}，入库 {record.ingested_at}）"
            )
    else:
        lines.append("  （从未纳入任何目标范围）")

    lines.append("回执时间线：")
    if explanation.execution_records:
        for view in explanation.execution_records:
            lines.append(
                f"  {view.execution_id}/{view.team_id} 启动 {view.started_at}，"
                f"报于 {view.reported_at}，回报量 {view.reported_count}，"
                f"复核状态 {view.review_state}"
                f"（记录 {view.record_id}，入库 {view.ingested_at}）"
            )
    else:
        lines.append("  （无任何回执）")

    lines.append("当前状态（按目标）：")
    if explanation.lines:
        caliber_labels = {"current": "当前口径", "superseded": "被替换口径"}
        for line in explanation.lines:
            status = line.status
            lines.append(
                f"  {line.objective.objective_id}（{caliber_labels[line.caliber]}）："
                f"{STATUS_LABELS[status.status]}（{status.reason}）"
            )
    else:
        lines.append("  （无关联目标）")
    return "\n".join(lines)


def render_unverified(pairs: list[tuple[str, ItemStatusView]], as_of: str | None = None) -> str:
    lines = [f"未核验事项清单（记录时点：{_as_of_label(as_of)}）"]
    if not pairs:
        lines.append("  无：范围内事项均有已验收回执。")
        return "\n".join(lines)
    for stage_id, item in pairs:
        lines.append(
            f"  {stage_id} / {item.objective_id} / {item.work_item_id}："
            f"{STATUS_LABELS[item.status]}（{item.reason}）"
        )
    lines.append("提示：以上事项证据不足，不能推断工作已完成。")
    return "\n".join(lines)
