from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from campaign_milestones.cli import main as cli_main  # noqa: E402
from campaign_milestones.ingest import ingest_batch  # noqa: E402
from campaign_milestones.queries import (  # noqa: E402
    STATUS_NO_EVIDENCE,
    STATUS_OUT_OF_SCOPE,
    STATUS_PAUSED,
    STATUS_REPORTED_UNVERIFIED,
    STATUS_RETURNED,
    STATUS_VERIFIED,
    explain_item,
    item_status,
    list_stages,
    stage_conclusion,
    unverified_items,
)
from campaign_milestones.schema import connect  # noqa: E402

SOURCES = ROOT / "sources"
UPDATES = SOURCES / "updates" / "2026-09-20"

T_EARLY = "2026-09-10T00:00:00Z"  # 任何批次入库之前
T_BATCH1 = "2026-09-18T12:00:00Z"  # 初始批次入库后
T_BATCH2 = "2026-09-20T12:00:00Z"  # 追加批次入库后


def build_conn(with_updates: bool = True) -> sqlite3.Connection:
    conn = connect(":memory:")
    ingest_batch(conn, label="初始批次", ingested_at=T_BATCH1, directory=SOURCES)
    if with_updates:
        ingest_batch(conn, label="追加批次", ingested_at=T_BATCH2, directory=UPDATES)
    return conn


def item_map(conclusion, objective_id):
    for progress in conclusion.confirmed_objectives:
        if progress.objective.objective_id == objective_id:
            return {item.work_item_id: item for item in progress.items}
    raise AssertionError(f"未找到已确认目标 {objective_id}")


def superseded_map(conclusion, objective_id):
    for progress in conclusion.superseded_objectives:
        if progress.objective.objective_id == objective_id:
            return {item.work_item_id: item for item in progress.items}
    raise AssertionError(f"未找到被替换目标 {objective_id}")


class StageCaliberTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = build_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_as_of_before_any_batch_has_no_record(self) -> None:
        conclusion = stage_conclusion(self.conn, "STG-1", as_of=T_EARLY)
        self.assertFalse(conclusion.exists)

    def test_initial_batch_caliber(self) -> None:
        conclusion = stage_conclusion(self.conn, "STG-1", as_of=T_BATCH1)
        self.assertTrue(conclusion.exists)
        self.assertEqual(conclusion.revision.revision, 2)
        self.assertFalse(conclusion.post_deadline_adjustment)
        # 当前口径只含 OBJ-2；OBJ-1 属被替换口径
        self.assertEqual(
            [p.objective.objective_id for p in conclusion.confirmed_objectives], ["OBJ-2"]
        )
        self.assertEqual(
            [p.objective.objective_id for p in conclusion.superseded_objectives], ["OBJ-1"]
        )
        items = item_map(conclusion, "OBJ-2")
        self.assertEqual(items["CASE-8"].status, STATUS_NO_EVIDENCE)
        self.assertEqual(items["CASE-9"].status, STATUS_RETURNED)
        old = superseded_map(conclusion, "OBJ-1")
        self.assertEqual(old["CASE-7"].status, STATUS_REPORTED_UNVERIFIED)
        self.assertTrue(old["CASE-7"].late)  # 报于 09-18，晚于修订 1 截止 09-15
        self.assertTrue(old["CASE-7"].any_accepted)  # TEAM-A 的验收记录仍在

    def test_draft_objective_stays_out_of_denominator(self) -> None:
        conclusion = stage_conclusion(self.conn, "STG-2", as_of=T_BATCH1)
        self.assertTrue(conclusion.exists)
        self.assertEqual(conclusion.confirmed_objectives, [])
        self.assertEqual(
            [obj.objective_id for obj in conclusion.pending_objectives], ["OBJ-3"]
        )
        self.assertEqual(conclusion.unverified_items, [])

    def test_current_caliber_reflects_latest_confirmed_revision(self) -> None:
        conclusion = stage_conclusion(self.conn, "STG-1")
        self.assertEqual(conclusion.revision.revision, 3)
        # 修订 3 入库于 09-20，晚于修订 2 截止 09-18 → 截止后调整
        self.assertTrue(conclusion.post_deadline_adjustment)
        self.assertEqual(conclusion.previous_due_at, "2026-09-18T00:00:00Z")
        progress = conclusion.confirmed_objectives[0]
        self.assertEqual(progress.objective.objective_id, "OBJ-2")
        self.assertEqual(progress.objective.expected_count, 10)  # 新口径的期望值
        items = {item.work_item_id: item for item in progress.items}
        self.assertEqual(list(items), ["CASE-9"])  # CASE-8 已移出，不进分母
        self.assertEqual(items["CASE-9"].status, STATUS_REPORTED_UNVERIFIED)
        removed = {item.work_item_id: item for item in progress.removed_items}
        self.assertEqual(removed["CASE-8"].status, STATUS_OUT_OF_SCOPE)
        # 范围外证据：CASE-11 从未纳入 OBJ-2 范围
        self.assertEqual([r.work_item_id for r in progress.orphan_receipts], ["CASE-11"])

    def test_superseded_progress_is_preserved_not_merged(self) -> None:
        conclusion = stage_conclusion(self.conn, "STG-1")
        old = superseded_map(conclusion, "OBJ-1")
        # CASE-7 的 EX-12 已验收，但 OBJ-1 属历史口径，只备查不并入分母
        self.assertEqual(old["CASE-7"].status, STATUS_VERIFIED)
        self.assertNotIn("CASE-7", item_map(conclusion, "OBJ-2"))

    def test_stage2_current_caliber(self) -> None:
        conclusion = stage_conclusion(self.conn, "STG-2")
        self.assertEqual(
            [p.objective.objective_id for p in conclusion.confirmed_objectives], ["OBJ-3"]
        )
        items = item_map(conclusion, "OBJ-3")
        self.assertEqual(items["CASE-9"].status, STATUS_PAUSED)
        self.assertEqual(items["CASE-10"].status, STATUS_NO_EVIDENCE)

    def test_list_stages(self) -> None:
        stages = {rev.stage_id: rev for rev in list_stages(self.conn)}
        self.assertEqual(stages["STG-1"].revision, 3)
        self.assertEqual(stages["STG-2"].revision, 1)
        earlier = {rev.stage_id: rev for rev in list_stages(self.conn, as_of=T_BATCH1)}
        self.assertEqual(earlier["STG-1"].revision, 2)


class HistoryPreservationTests(unittest.TestCase):
    """追加批次后，历史时点的口径与进度必须保持不变。"""

    def test_as_of_result_is_stable_across_appends(self) -> None:
        before = build_conn(with_updates=False)
        after = build_conn(with_updates=True)
        try:
            snap_before = stage_conclusion(before, "STG-1", as_of=T_BATCH1)
            snap_after = stage_conclusion(after, "STG-1", as_of=T_BATCH1)
            self.assertEqual(snap_before.revision.record_id, snap_after.revision.record_id)

            def statuses(conclusion):
                result = {}
                for group in (conclusion.confirmed_objectives, conclusion.superseded_objectives):
                    for progress in group:
                        for item in progress.items:
                            result[(progress.objective.objective_id, item.work_item_id)] = item.status
                return result
            self.assertEqual(statuses(snap_before), statuses(snap_after))
        finally:
            before.close()
            after.close()

    def test_raw_layer_is_append_only(self) -> None:
        conn = build_conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE raw_execution_record SET review_state = 'accepted' "
                    "WHERE record_id = 'execution-03'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM raw_stage_record WHERE record_id = 'stages-01'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM ingest_batch WHERE batch_id = 1")
        finally:
            conn.close()


class IngestTests(unittest.TestCase):
    def test_duplicate_record_id_rejected(self) -> None:
        conn = build_conn()
        try:
            with self.assertRaises(ValueError):
                ingest_batch(conn, label="重复", ingested_at=T_BATCH2, directory=SOURCES)
        finally:
            conn.close()

    def test_invalid_enum_rejected(self) -> None:
        conn = connect(":memory:")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "execution.csv"
                path.write_text(
                    "record_id,execution_id,objective_id,work_item_id,team_id,"
                    "started_at,reported_at,reported_count,review_state\n"
                    "execution-x,EX-X,OBJ-1,CASE-7,TEAM-A,2026-09-02T10:00:00Z,"
                    "2026-09-03T10:00:00Z,1,bogus\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    ingest_batch(
                        conn,
                        label="坏数据",
                        ingested_at=T_BATCH1,
                        files={"execution": path},
                    )
            # 失败的批次整体回滚，不留半个批次
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ingest_batch").fetchone()[0], 0)
        finally:
            conn.close()

    def test_confirmed_objective_requires_confirmed_at(self) -> None:
        conn = connect(":memory:")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "objectives.csv"
                path.write_text(
                    "record_id,objective_id,stage_id,stage_revision,measure_code,"
                    "expected_count,confirmed_at,objective_state\n"
                    "objectives-x,OBJ-X,STG-1,1,reviewed_cases,5,,confirmed\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    ingest_batch(
                        conn,
                        label="缺确认时间",
                        ingested_at=T_BATCH1,
                        files={"objectives": path},
                    )
        finally:
            conn.close()


class ItemExplanationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = build_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_handoff_item_case7(self) -> None:
        explanation = explain_item(self.conn, "CASE-7")
        self.assertEqual(explanation.handoff_count, 2)
        self.assertEqual(explanation.teams, ["TEAM-A", "TEAM-B"])
        # EX-11 一条 + EX-12 两个版本（迟报、验收）
        self.assertEqual(len(explanation.execution_records), 3)
        line = {l.objective.objective_id: l for l in explanation.lines}["OBJ-1"]
        self.assertEqual(line.caliber, "superseded")
        self.assertEqual(line.status.status, STATUS_VERIFIED)

    def test_returned_then_resubmitted_item_case9(self) -> None:
        explanation = explain_item(self.conn, "CASE-9")
        states = [view.review_state for view in explanation.execution_records
                  if view.execution_id == "EX-13"]
        self.assertEqual(states, ["returned", "submitted"])  # 退回后重报
        lines = {l.objective.objective_id: l for l in explanation.lines}
        self.assertEqual(lines["OBJ-2"].status.status, STATUS_REPORTED_UNVERIFIED)
        self.assertEqual(lines["OBJ-3"].status.status, STATUS_PAUSED)

    def test_explain_as_of_hides_later_versions(self) -> None:
        explanation = explain_item(self.conn, "CASE-7", as_of=T_BATCH1)
        self.assertEqual(len(explanation.execution_records), 2)  # 验收版本尚未入库
        line = {l.objective.objective_id: l for l in explanation.lines}["OBJ-1"]
        self.assertEqual(line.status.status, STATUS_REPORTED_UNVERIFIED)

    def test_item_status_for_out_of_scope(self) -> None:
        status = item_status(self.conn, "OBJ-2", "CASE-8")
        self.assertEqual(status.status, STATUS_OUT_OF_SCOPE)


class UnverifiedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = build_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_unverified_items_current(self) -> None:
        found = {
            (stage, item.objective_id, item.work_item_id, item.status)
            for stage, item in unverified_items(self.conn)
        }
        self.assertIn(("STG-1", "OBJ-2", "CASE-9", STATUS_REPORTED_UNVERIFIED), found)
        self.assertIn(("STG-2", "OBJ-3", "CASE-9", STATUS_PAUSED), found)
        self.assertIn(("STG-2", "OBJ-3", "CASE-10", STATUS_NO_EVIDENCE), found)
        # CASE-7 已验收，不得出现在未核验清单
        self.assertFalse(any(entry[2] == "CASE-7" for entry in found))

    def test_unverified_items_as_of_initial_batch(self) -> None:
        found = {
            (stage, item.objective_id, item.work_item_id, item.status)
            for stage, item in unverified_items(self.conn, as_of=T_BATCH1)
        }
        self.assertIn(("STG-1", "OBJ-2", "CASE-9", STATUS_RETURNED), found)
        self.assertIn(("STG-1", "OBJ-2", "CASE-8", STATUS_NO_EVIDENCE), found)
        # STG-2 目标仍是草稿，没有已确认口径，不产生未核验事项
        self.assertFalse(any(entry[0] == "STG-2" for entry in found))


class CliTests(unittest.TestCase):
    def test_demo_runs_and_reports(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = cli_main(["demo", "--db", ":memory:"])
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("阶段 STG-1 结论复核", output)
        self.assertIn("截止后调整", output)
        self.assertIn("事项 CASE-7 说明", output)
        self.assertIn("未核验事项清单", output)

    def test_report_stage_with_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "wh.db")
            self.assertEqual(cli_main(["init", db]), 0)
            self.assertEqual(
                cli_main(
                    ["ingest", db, "--dir", str(SOURCES),
                     "--label", "初始批次", "--at", T_BATCH1]
                ),
                0,
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(
                    cli_main(["report-stage", db, "STG-1", "--as-of", T_BATCH1]), 0
                )
            self.assertIn("修订 2", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
