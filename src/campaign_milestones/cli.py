"""命令行入口：建库、追加批次、按 as-of 时点出报告。

用法示例（仓库根目录下）::

    PYTHONPATH=src python -m campaign_milestones demo --db :memory:
    PYTHONPATH=src python -m campaign_milestones init warehouse.db
    PYTHONPATH=src python -m campaign_milestones ingest warehouse.db \
        --dir sources --label 初始批次 --at 2026-09-18T12:00:00Z
    PYTHONPATH=src python -m campaign_milestones report-stage warehouse.db STG-1 \
        --as-of 2026-09-18T12:00:00Z
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ingest import ingest_batch
from .queries import explain_item, list_stages, stage_conclusion, unverified_items
from .report import (
    render_item_explanation,
    render_stage_conclusion,
    render_stage_list,
    render_unverified,
)
from .schema import connect

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 演示用批次：初始批次与 sources/updates/ 下各追加批次（按目录名排序）。
_DEMO_BASE_INGESTED_AT = "2026-09-18T12:00:00Z"


def _cmd_init(args: argparse.Namespace) -> int:
    connect(args.db).close()
    print(f"已初始化数仓：{args.db}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    batch_id = ingest_batch(
        conn,
        label=args.label,
        ingested_at=args.at,
        directory=args.dir,
        note=args.note or "",
    )
    print(f"批次 {batch_id} 已追加（{args.label}，记录时间 {args.at}）")
    return 0


def _cmd_stages(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    print(render_stage_list(list_stages(conn, args.as_of), args.as_of))
    return 0


def _cmd_report_stage(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    print(render_stage_conclusion(stage_conclusion(conn, args.stage, args.as_of)))
    return 0


def _cmd_report_item(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    print(render_item_explanation(explain_item(conn, args.item, args.as_of)))
    return 0


def _cmd_unverified(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    pairs = unverified_items(conn, stage_id=args.stage, as_of=args.as_of)
    print(render_unverified(pairs, args.as_of))
    return 0


def _ingest_demo_sources(conn) -> None:
    """把仓库自带的两批合成资料入库：初始批次 + sources/updates/ 追加批次。"""
    sources = REPO_ROOT / "sources"
    ingest_batch(conn, label="初始批次", ingested_at=_DEMO_BASE_INGESTED_AT, directory=sources)
    updates_root = sources / "updates"
    if updates_root.is_dir():
        for subdir in sorted(path for path in updates_root.iterdir() if path.is_dir()):
            manifest_path = subdir / "batch.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            ingest_batch(
                conn,
                label=manifest["label"],
                ingested_at=manifest["ingested_at"],
                directory=subdir,
            )


def _cmd_demo(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    _ingest_demo_sources(conn)
    sections = [
        render_stage_list(list_stages(conn)),
        render_stage_conclusion(
            stage_conclusion(conn, "STG-1", as_of=_DEMO_BASE_INGESTED_AT)
        ),
        render_stage_conclusion(stage_conclusion(conn, "STG-1")),
        render_stage_conclusion(stage_conclusion(conn, "STG-2")),
        render_item_explanation(explain_item(conn, "CASE-7")),
        render_item_explanation(explain_item(conn, "CASE-9")),
        render_unverified(unverified_items(conn)),
    ]
    print("\n\n".join(sections))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campaign_milestones",
        description="治理阶段进度数仓：仅追加原始层 + as-of 口径解析",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="创建数仓文件")
    p_init.add_argument("db", help="SQLite 文件路径")
    p_init.set_defaults(func=_cmd_init)

    p_ingest = sub.add_parser("ingest", help="追加一个批次的来源 CSV")
    p_ingest.add_argument("db", help="SQLite 文件路径")
    p_ingest.add_argument("--dir", required=True, help="来源 CSV 所在目录")
    p_ingest.add_argument("--label", required=True, help="批次名称")
    p_ingest.add_argument("--at", required=True, help="批次记录时间（ISO 8601 带时区）")
    p_ingest.add_argument("--note", default="", help="批次备注")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_stages = sub.add_parser("stages", help="列出阶段及有效修订")
    p_stages.add_argument("db")
    p_stages.add_argument("--as-of", default=None, help="记录时点（缺省为当前最新）")
    p_stages.set_defaults(func=_cmd_stages)

    p_stage = sub.add_parser("report-stage", help="复核某阶段结论")
    p_stage.add_argument("db")
    p_stage.add_argument("stage", help="阶段编号，如 STG-1")
    p_stage.add_argument("--as-of", default=None, help="记录时点（缺省为当前最新）")
    p_stage.set_defaults(func=_cmd_report_stage)

    p_item = sub.add_parser("report-item", help="说明某工作事项")
    p_item.add_argument("db")
    p_item.add_argument("item", help="事项编号，如 CASE-7")
    p_item.add_argument("--as-of", default=None, help="记录时点（缺省为当前最新）")
    p_item.set_defaults(func=_cmd_report_item)

    p_unv = sub.add_parser("unverified", help="列出未核验事项")
    p_unv.add_argument("db")
    p_unv.add_argument("--stage", default=None, help="只列某阶段")
    p_unv.add_argument("--as-of", default=None, help="记录时点（缺省为当前最新）")
    p_unv.set_defaults(func=_cmd_unverified)

    p_demo = sub.add_parser("demo", help="用仓库自带合成资料演示完整流程")
    p_demo.add_argument("--db", default=":memory:", help="SQLite 路径（缺省内存库）")
    p_demo.set_defaults(func=_cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
