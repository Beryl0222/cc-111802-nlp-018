import argparse
import json
import sys
from pathlib import Path

from .board import BoardError, ReviewBoard
from .repository import RecordRepository
from .store import BoardStore
from .workflow import TextWorkflow


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parse_theme_spec(spec):
    parts = spec.split(":", 2)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise BoardError("新主题规格格式为 编号:名称[:关键词1,关键词2]")
    keywords = [k for k in parts[2].split(",") if k.strip()] if len(parts) == 3 else []
    return {"theme_id": parts[0], "label": parts[1], "keywords": keywords}


def _parse_mapping(pairs):
    mapping = {}
    for pair in pairs or []:
        if ":" not in pair:
            raise BoardError("归属映射格式为 问题编号:主题编号")
        question_id, theme_id = pair.split(":", 1)
        mapping[question_id] = theme_id
    return mapping


def _run_board(args):
    store = BoardStore(args.board)
    board = ReviewBoard(store.load())
    action = args.board_action
    if action == "round-open":
        result = board.open_round(note=args.note)
    elif action == "round-close":
        result = board.close_round()
    elif action == "question":
        result = board.upsert_question(args.question_id, args.text)
    elif action == "theme":
        result = board.add_theme(args.theme_id, args.label, args.keywords)
    elif action == "assign":
        result = board.assign_manual(args.question_id, args.theme_id, basis=args.basis)
    elif action == "autocluster":
        result = board.autocluster()
    elif action == "confirm":
        result = board.confirm_assignment(args.assignment_id)
    elif action == "merge":
        result = board.merge_themes(
            args.target, args.sources, label=args.label, keywords=args.keywords
        )
    elif action == "split":
        result = board.split_theme(
            args.source,
            [_parse_theme_spec(spec) for spec in args.new],
            mapping=_parse_mapping(args.map),
        )
    elif action == "followup":
        result = board.open_followup(args.question_id, theme_id=args.theme, note=args.note)
    elif action == "done":
        result = board.complete_followup(args.followup_id, note=args.note)
    elif action == "aggregate":
        result = board.aggregate()
    elif action == "trace":
        result = board.trace_question(args.question_id)
    elif action == "theme-trace":
        result = board.trace_theme(args.theme_id)
    elif action == "report":
        result = board.report()
    elif action == "events":
        result = board.round_events(args.round)
    else:  # pragma: no cover - argparse 已限定取值
        raise BoardError(f"未知操作 {action}")
    store.save(board.state)
    return result


def build_parser():
    parser = argparse.ArgumentParser(description="本地教研文本处理工具")
    sub = parser.add_subparsers(dest="action", required=True)

    p_ingest = sub.add_parser("ingest", help="导入文本记录")
    p_ingest.add_argument("path", nargs="?", default="fixtures/records.json")
    p_ingest.add_argument("--state", default="var/records.json")

    p_list = sub.add_parser("list", help="查看文本处理结果")
    p_list.add_argument("--state", default="var/records.json")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--board", default="var/board.json", help="归纳台状态文件")

    p_board = sub.add_parser("board", help="学生问题归纳台")
    board_sub = p_board.add_subparsers(dest="board_action", required=True)

    p = board_sub.add_parser("round-open", parents=[common], help="开启新一轮复盘")
    p.add_argument("--note", default="")

    board_sub.add_parser("round-close", parents=[common], help="关闭当前轮次")

    p = board_sub.add_parser("question", parents=[common], help="登记或补充问题文本")
    p.add_argument("question_id")
    p.add_argument("--text", required=True)

    p = board_sub.add_parser("theme", parents=[common], help="新建主题")
    p.add_argument("theme_id")
    p.add_argument("--label", required=True)
    p.add_argument("--keywords", nargs="*", default=[])

    p = board_sub.add_parser("assign", parents=[common], help="教师手工指定归属")
    p.add_argument("question_id")
    p.add_argument("theme_id")
    p.add_argument("--basis", default="")

    board_sub.add_parser("autocluster", parents=[common], help="按关键词自动聚类")

    p = board_sub.add_parser("confirm", parents=[common], help="确认待复核归属")
    p.add_argument("assignment_id")

    p = board_sub.add_parser("merge", parents=[common], help="合并主题")
    p.add_argument("target")
    p.add_argument("sources", nargs="+")
    p.add_argument("--label", default=None)
    p.add_argument("--keywords", nargs="*", default=None)

    p = board_sub.add_parser("split", parents=[common], help="拆分主题")
    p.add_argument("source")
    p.add_argument(
        "--new", nargs="+", action="extend", default=[], required=True,
        metavar="编号:名称[:关键词1,关键词2]",
    )
    p.add_argument(
        "--map", nargs="+", action="extend", default=[], metavar="问题编号:主题编号"
    )

    p = board_sub.add_parser("followup", parents=[common], help="建立教师跟进")
    p.add_argument("question_id")
    p.add_argument("--theme", default=None)
    p.add_argument("--note", default="")

    p = board_sub.add_parser("done", parents=[common], help="完成教师跟进")
    p.add_argument("followup_id")
    p.add_argument("--note", default="")

    board_sub.add_parser("aggregate", parents=[common], help="按当前口径汇总")

    p = board_sub.add_parser("trace", parents=[common], help="追溯单条问题")
    p.add_argument("question_id")

    p = board_sub.add_parser("theme-trace", parents=[common], help="追溯主题生命周期")
    p.add_argument("theme_id")

    board_sub.add_parser("report", parents=[common], help="查看待办总览")

    p = board_sub.add_parser("events", parents=[common], help="查看事件日志")
    p.add_argument("--round", type=int, default=None)

    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.action == "ingest":
            workflow = TextWorkflow(RecordRepository(args.state))
            result = workflow.ingest(json.loads(Path(args.path).read_text(encoding="utf-8")))
        elif args.action == "list":
            workflow = TextWorkflow(RecordRepository(args.state))
            result = workflow.list_records()
        else:
            result = _run_board(args)
    except (BoardError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    _print(result)


if __name__ == "__main__":
    main()
