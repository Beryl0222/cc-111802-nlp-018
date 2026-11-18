import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nlp_student_question_cluster.board import (
    AGGREGATION_BASIS,
    BoardError,
    ReviewBoard,
    default_state,
)
from nlp_student_question_cluster.store import BoardStore


class FakeClock:
    def __init__(self):
        self.moment = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.moment += timedelta(minutes=1)
        return self.moment


def make_board(state=None):
    return ReviewBoard(state=state, clock=FakeClock())


class RoundTests(unittest.TestCase):
    def test_mutations_require_an_open_round(self):
        board = make_board()
        with self.assertRaises(BoardError):
            board.upsert_question("q1", "怎么换算单位？")
        with self.assertRaises(BoardError):
            board.aggregate()
        with self.assertRaises(BoardError):
            board.close_round()

    def test_rounds_are_sequential_and_events_carry_round(self):
        board = make_board()
        first = board.open_round(note="第一轮复盘")
        self.assertEqual(first["round"], 1)
        with self.assertRaises(BoardError):
            board.open_round()
        board.close_round()
        second = board.open_round()
        self.assertEqual(second["round"], 2)
        kinds = [(e["kind"], e["round"]) for e in board.round_events()]
        self.assertIn(("round_opened", 1), kinds)
        self.assertIn(("round_closed", 1), kinds)
        self.assertIn(("round_opened", 2), kinds)
        self.assertEqual([e["seq"] for e in board.round_events()], [1, 2, 3])
        self.assertEqual(len(board.round_events(1)), 2)


class QuestionAndAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.board = make_board()
        self.board.open_round()
        self.board.add_theme("t-unit", "单位换算", keywords=["单位", "换算", "千米"])
        self.board.add_theme("t-formula", "公式选择", keywords=["公式", "定理"])

    def test_autocluster_assigns_multiple_themes_for_one_question(self):
        self.board.upsert_question("q1", "单位换算时该选哪个公式？")
        summary = self.board.autocluster()
        self.assertEqual(len(summary["created"]), 2)
        live = self.board._live_assignments(question_id="q1")
        self.assertEqual({a["theme_id"] for a in live}, {"t-unit", "t-formula"})
        self.assertTrue(all(a["origin"] == "auto" for a in live))

    def test_autocluster_is_idempotent_and_never_duplicates(self):
        self.board.upsert_question("q1", "千米和米怎么换算？")
        self.board.autocluster()
        again = self.board.autocluster()
        self.assertEqual(again["created"], [])
        self.assertEqual(len(self.board._live_assignments(question_id="q1")), 1)

    def test_manual_assignment_overrides_auto(self):
        self.board.upsert_question("q1", "千米和米怎么换算？")
        self.board.autocluster()
        manual = self.board.assign_manual("q1", "t-unit", basis="教研员指定")
        live = self.board._live_assignments(question_id="q1")
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["origin"], "manual")
        self.assertEqual(live[0]["assignment_id"], manual["assignment_id"])
        superseded = [
            a for a in self.board.state["assignments"] if a["status"] == "superseded"
        ]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["origin"], "auto")

    def test_autocluster_keeps_manual_and_can_add_other_themes(self):
        self.board.upsert_question("q1", "单位换算时该选哪个公式？")
        self.board.assign_manual("q1", "t-unit")
        self.board.autocluster()
        live = {a["theme_id"]: a for a in self.board._live_assignments(question_id="q1")}
        self.assertEqual(live["t-unit"]["origin"], "manual")
        self.assertEqual(live["t-formula"]["origin"], "auto")

    def test_text_supplement_flags_assignments_for_review(self):
        self.board.upsert_question("q1", "千米和米怎么换算？")
        self.board.autocluster()
        self.board.close_round()
        self.board.open_round()
        result = self.board.upsert_question("q1", "千米和米怎么换算？还要选对公式。")
        self.assertTrue(result["changed"])
        self.assertEqual(result["question"]["revision"], 2)
        self.assertEqual(len(result["flagged"]), 1)
        live = self.board._live_assignments(question_id="q1")
        self.assertEqual(live[0]["status"], "needs_review")
        # 自动聚类复核：原主题仍匹配则确认，新出现的主题补充建档
        summary = self.board.autocluster()
        self.assertEqual(len(summary["confirmed"]), 1)
        self.assertEqual(len(summary["created"]), 1)
        statuses = {a["theme_id"]: a["status"] for a in self.board._live_assignments(question_id="q1")}
        self.assertEqual(statuses, {"t-unit": "active", "t-formula": "active"})

    def test_autocluster_drops_auto_assignment_that_no_longer_matches(self):
        self.board.upsert_question("q1", "千米和米怎么换算？")
        self.board.autocluster()
        self.board.close_round()
        self.board.open_round()
        self.board.upsert_question("q1", "这道题的公式应该怎么选？")
        summary = self.board.autocluster()
        self.assertEqual(len(summary["dropped"]), 1)
        live = self.board._live_assignments(question_id="q1")
        self.assertEqual([a["theme_id"] for a in live], ["t-formula"])

    def test_manual_assignment_waits_for_teacher_confirmation(self):
        self.board.upsert_question("q1", "千米和米怎么换算？")
        manual = self.board.assign_manual("q1", "t-unit")
        self.board.close_round()
        self.board.open_round()
        self.board.upsert_question("q1", "千米和米怎么换算？（补充了图形）")
        summary = self.board.autocluster()
        self.assertEqual(summary["manual_pending"], [manual["assignment_id"]])
        self.assertEqual(summary["confirmed"], [])
        self.assertEqual(summary["dropped"], [])
        still = self.board._live_assignments(question_id="q1")[0]
        self.assertEqual(still["status"], "needs_review")
        self.board.confirm_assignment(manual["assignment_id"])
        self.assertEqual(self.board._live_assignments(question_id="q1")[0]["status"], "active")

    def test_same_text_upsert_is_a_noop(self):
        self.board.upsert_question("q1", "  千米和米\n怎么换算？ ")
        again = self.board.upsert_question("q1", "千米和米 怎么换算？")
        self.assertFalse(again["changed"])
        self.assertEqual(again["question"]["revision"], 1)


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.board = make_board()
        self.board.open_round()
        self.board.add_theme("t-unit", "单位换算", keywords=["单位", "换算"])
        self.board.add_theme("t-formula", "公式选择", keywords=["公式"])
        self.board.upsert_question("q1", "单位换算时该选哪个公式？")
        self.board.autocluster()

    def test_followup_requires_unambiguous_theme(self):
        with self.assertRaises(BoardError):
            self.board.open_followup("q1")
        followup = self.board.open_followup("q1", theme_id="t-unit", note="安排补讲")
        self.assertEqual(followup["status"], "pending")
        self.assertEqual(followup["theme_label"], "单位换算")
        with self.assertRaises(BoardError):
            self.board.open_followup("q1", theme_id="t-missing")
        with self.assertRaises(BoardError):
            self.board.open_followup("q-unknown", theme_id="t-unit")

    def test_complete_followup_records_round_and_revision(self):
        followup = self.board.open_followup("q1", theme_id="t-unit")
        done = self.board.complete_followup(followup["followup_id"], note="已补讲")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["closed_round"], 1)
        self.assertEqual(done["closed_question_revision"], 1)
        with self.assertRaises(BoardError):
            self.board.complete_followup(followup["followup_id"])

    def test_completed_followup_is_not_migrated_by_text_update(self):
        followup = self.board.open_followup("q1", theme_id="t-unit")
        self.board.complete_followup(followup["followup_id"])
        self.board.close_round()
        self.board.open_round()
        self.board.upsert_question("q1", "单位换算时该选哪个公式？（补充了单位表）")
        kept = self.board._followup(followup["followup_id"])
        self.assertEqual(kept["status"], "done")
        self.assertEqual(kept["theme_id"], "t-unit")
        self.assertEqual(len(kept["trail"]), 1)
        report = self.board.report()
        self.assertEqual(report["completed_followups_on_updated_questions"], [followup["followup_id"]])


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.board = make_board()
        self.board.open_round()
        self.board.add_theme("t-unit", "单位换算", keywords=["单位", "换算"])
        self.board.add_theme("t-scale", "度量单位", keywords=["度量", "进率"])
        self.board.upsert_question("q1", "千米和米怎么换算？")
        self.board.upsert_question("q2", "度量单位之间怎么进位？")
        self.board.upsert_question("q3", "换算进率是多少？")
        self.board.autocluster()
        # q1 只在 t-unit；q2 同时在两个主题（t-scale 为手工）；q3 同时在两个主题（均自动）
        self.board.assign_manual("q2", "t-scale")

    def test_merge_dedupes_questions_and_keeps_manual_origin(self):
        result = self.board.merge_themes("t-measure", ["t-unit", "t-scale"], label="度量与换算")
        self.assertTrue(result["deduped"])
        live = self.board._live_assignments(theme_id="t-measure")
        self.assertEqual(sorted({a["question_id"] for a in live}), ["q1", "q2", "q3"])
        q2 = [a for a in live if a["question_id"] == "q2"]
        self.assertEqual(len(q2), 1)
        self.assertEqual(q2[0]["origin"], "manual")
        snapshot = self.board.aggregate()
        self.assertEqual(snapshot["themes"]["t-measure"]["count"], 3)
        self.assertEqual(snapshot["total_questions"], 3)
        self.assertNotIn("t-unit", snapshot["themes"])

    def test_merge_records_lineage_and_repoints_only_pending_followups(self):
        pending = self.board.open_followup("q1", theme_id="t-unit")
        done = self.board.open_followup("q2", theme_id="t-scale")
        self.board.complete_followup(done["followup_id"])
        self.board.merge_themes("t-measure", ["t-unit", "t-scale"], label="度量与换算")
        moved = self.board._followup(pending["followup_id"])
        self.assertEqual(moved["theme_id"], "t-measure")
        self.assertEqual([step["theme_id"] for step in moved["trail"]], ["t-unit", "t-measure"])
        untouched = self.board._followup(done["followup_id"])
        self.assertEqual(untouched["theme_id"], "t-scale")
        self.assertEqual(len(untouched["trail"]), 1)
        source = self.board._theme("t-unit")
        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["successors"], ["t-measure"])
        self.assertEqual(self.board._theme("t-measure")["predecessors"], ["t-unit", "t-scale"])

    def test_merge_carries_needs_review_state(self):
        self.board.close_round()
        self.board.open_round()
        self.board.upsert_question("q1", "千米和米怎么换算？（补充了例题）")
        self.board.merge_themes("t-measure", ["t-unit", "t-scale"], label="度量与换算")
        carried = [
            a for a in self.board._live_assignments(theme_id="t-measure")
            if a["question_id"] == "q1"
        ]
        self.assertEqual(carried[0]["status"], "needs_review")
        report = self.board.report()
        self.assertEqual([item["question_id"] for item in report["needs_review"]], ["q1"])

    def test_merge_rejects_invalid_targets(self):
        with self.assertRaises(BoardError):
            self.board.merge_themes("t-unit", ["t-unit"])
        with self.assertRaises(BoardError):
            self.board.merge_themes("t-new", [], label="空")


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.board = make_board()
        self.board.open_round()
        self.board.add_theme("t-mix", "单位与公式", keywords=["单位", "公式", "进率"])
        self.board.upsert_question("q1", "千米和米这些单位怎么换算？")
        self.board.upsert_question("q2", "这道题该用哪个公式？")
        self.board.upsert_question("q3", "进率到底怎么算？")
        self.board.autocluster()
        self.followup = self.board.open_followup("q1", theme_id="t-mix", note="已记录")
        self.board.complete_followup(self.followup["followup_id"])
        self.board.aggregate()
        self.board.close_round()
        self.board.open_round()

    def split(self, mapping=None):
        return self.board.split_theme(
            "t-mix",
            [
                {"theme_id": "t-unit", "label": "单位换算", "keywords": ["单位", "换算"]},
                {"theme_id": "t-formula", "label": "公式选择", "keywords": ["公式"]},
            ],
            mapping=mapping,
        )

    def test_split_reassigns_by_autocluster_and_reports_unassigned(self):
        result = self.split()
        self.assertEqual(result["unassigned"], ["q3"])
        live = self.board._live_assignments()
        by_question = {}
        for assignment in live:
            by_question.setdefault(assignment["question_id"], set()).add(assignment["theme_id"])
        self.assertEqual(by_question["q1"], {"t-unit"})
        self.assertEqual(by_question["q2"], {"t-formula"})
        source = self.board._theme("t-mix")
        self.assertEqual(source["status"], "split")
        self.assertEqual(source["successors"], ["t-unit", "t-formula"])

    def test_split_keeps_completed_followup_traceable_to_question(self):
        self.split()
        kept = self.board._followup(self.followup["followup_id"])
        self.assertEqual(kept["status"], "done")
        self.assertEqual(kept["theme_id"], "t-mix")
        self.assertEqual(kept["theme_label"], "单位与公式")
        trace = self.board.trace_question("q1")
        self.assertEqual([f["followup_id"] for f in trace["followups"]], [self.followup["followup_id"]])
        self.assertEqual(trace["followups"][0]["theme_id"], "t-mix")
        theme_trace = self.board.trace_theme("t-mix")
        self.assertEqual([f["followup_id"] for f in theme_trace["followups"]], [self.followup["followup_id"]])

    def test_split_respects_manual_mapping(self):
        result = self.split(mapping={"q3": "t-formula"})
        self.assertEqual(result["unassigned"], [])
        q3 = self.board._live_assignments(question_id="q3")
        self.assertEqual(q3[0]["theme_id"], "t-formula")
        self.assertEqual(q3[0]["origin"], "manual")

    def test_split_rejects_bad_mapping(self):
        with self.assertRaises(BoardError):
            self.split(mapping={"q3": "t-unknown"})
        with self.assertRaises(BoardError):
            self.split(mapping={"q-unknown": "t-unit"})

    def test_historical_aggregation_snapshot_is_preserved(self):
        self.split()
        self.board.aggregate()
        snapshots = self.board.state["aggregations"]
        self.assertEqual(len(snapshots), 2)
        first, second = snapshots
        self.assertEqual(first["round"], 1)
        self.assertEqual(first["themes"]["t-mix"]["count"], 3)
        self.assertEqual(second["round"], 2)
        self.assertNotIn("t-mix", second["themes"])
        self.assertEqual(second["themes"]["t-unit"]["count"], 1)
        self.assertEqual(second["themes"]["t-formula"]["count"], 1)
        self.assertEqual(second["total_questions"], 2)
        self.assertEqual(second["unassigned"], ["q3"])
        self.assertEqual(first["basis"], AGGREGATION_BASIS)


class AggregationTests(unittest.TestCase):
    def test_multi_theme_question_counts_once_per_theme_and_once_in_total(self):
        board = make_board()
        board.open_round()
        board.add_theme("t-unit", "单位换算", keywords=["单位"])
        board.add_theme("t-formula", "公式选择", keywords=["公式"])
        board.upsert_question("q1", "单位换算时该选哪个公式？")
        board.upsert_question("q2", "单位怎么写？")
        board.upsert_question("q3", "还没人看")
        board.autocluster()
        snapshot = board.aggregate()
        self.assertEqual(snapshot["themes"]["t-unit"]["count"], 2)
        self.assertEqual(snapshot["themes"]["t-formula"]["count"], 1)
        self.assertEqual(snapshot["total_questions"], 2)
        self.assertEqual(snapshot["unassigned"], ["q3"])
        self.assertEqual(snapshot["themes"]["t-unit"]["by_origin"], {"manual": 0, "auto": 2})

    def test_needs_review_questions_stay_counted_and_flagged(self):
        board = make_board()
        board.open_round()
        board.add_theme("t-unit", "单位换算", keywords=["单位"])
        board.upsert_question("q1", "单位怎么写？")
        board.autocluster()
        board.upsert_question("q1", "单位怎么写？（补充）")
        snapshot = board.aggregate()
        self.assertEqual(snapshot["themes"]["t-unit"]["count"], 1)
        self.assertEqual(snapshot["themes"]["t-unit"]["needs_review"], ["q1"])


class TraceAndStoreTests(unittest.TestCase):
    def test_trace_question_covers_text_assignments_followups_and_aggregations(self):
        board = make_board()
        board.open_round()
        board.add_theme("t-unit", "单位换算", keywords=["单位"])
        board.upsert_question("q1", "单位怎么写？")
        board.autocluster()
        board.open_followup("q1", theme_id="t-unit")
        board.aggregate()
        board.close_round()
        board.open_round()
        board.upsert_question("q1", "单位怎么写？（补充）")
        trace = board.trace_question("q1")
        self.assertEqual([h["revision"] for h in trace["question"]["history"]], [1, 2])
        self.assertEqual(trace["question"]["history"][1]["round"], 2)
        self.assertEqual(len(trace["assignments"]), 1)
        self.assertEqual(trace["assignments"][0]["status"], "needs_review")
        self.assertEqual(trace["assignments"][0]["theme_label"], "单位换算")
        self.assertEqual(len(trace["followups"]), 1)
        self.assertEqual(trace["aggregation_appearances"][0]["themes"], ["t-unit"])

    def test_store_roundtrip_keeps_board_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BoardStore(Path(directory) / "board.json")
            board = ReviewBoard(store.load(), clock=FakeClock())
            board.open_round(note="第一轮")
            board.add_theme("t-unit", "单位换算", keywords=["单位"])
            board.upsert_question("q1", "单位怎么写？")
            board.autocluster()
            board.aggregate()
            store.save(board.state)

            loaded = ReviewBoard(store.load(), clock=FakeClock())
            self.assertEqual(loaded.report()["questions"], 1)
            loaded.close_round()
            loaded.open_round()
            snapshot = loaded.aggregate()
            self.assertEqual(snapshot["round"], 2)
            self.assertEqual(snapshot["themes"]["t-unit"]["count"], 1)
            store.save(loaded.state)
            again = BoardStore(Path(directory) / "board.json").load()
            self.assertEqual(len(again["aggregations"]), 2)

    def test_store_rejects_invalid_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.json"
            path.write_text('{"schema": 2}', encoding="utf-8")
            with self.assertRaises(ValueError):
                BoardStore(path).load()

    def test_default_state_is_json_serializable_and_independent(self):
        first, second = default_state(), default_state()
        first["counters"]["event"] = 9
        self.assertEqual(second["counters"]["event"], 0)


if __name__ == "__main__":
    unittest.main()
