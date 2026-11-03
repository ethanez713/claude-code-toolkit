import shutil
from pathlib import Path

from orchestrator.board import scan
from orchestrator.plans import open_gate_id, parse_plan, runnable_rows
from support import TempCase


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_index.md"

CELLS = [
    ("01", "GATE r6", "r6"),
    ("02", "GATE G2 (built 4bd25c2)", "G2"),
    ("03", "Steps 1\u20132 DONE fe98b2b, 0463f50; step 3 /welcome held (needs a schema flag); GATE G4", "G4"),
    ("04", "Steps 1\u20132 DONE 9bfbbf7; step 3 at G6; GATE G6", "G6"),
    ("05", "GATE-L0 \u2014 12.1\u201312.4 landed (the S20U ...)", "L0"),
    ("06", "DONE 99a14c3 (GATE-A passed: pts 1 and 3)", None),
    ("07", "TODO (1080p30 default landed early: 22d86ee; GATE-D accepted)", None),
    ("08", "DONE c101a58 (GATE-F accepted; the siren is confirmed loud)", None),
    ("09", "DONE 1eb7e6b (GATE-SEC passed: see its row)", None),
]


class GateCellTest(TempCase):
    def test_given_status_cells_when_detected_then_only_open_gates_match(self):
        text = FIXTURE.read_text(encoding="utf-8")
        plan = parse_plan(text)
        rows = {row.row_id: row for row in plan.rows}
        for row_id, cell, gate in CELLS:
            with self.subTest(row_id=row_id):
                self.assertIn(cell, text)
                self.assertEqual(rows[row_id].status, cell)
                self.assertEqual(open_gate_id(cell), gate)
                self.assertEqual(rows[row_id].gate, gate)
        self.assertEqual(plan.open_gate_ids(), {"r6", "G2", "G4", "G6", "L0"})
        self.assertEqual(
            {row.row_id for row in runnable_rows(plan)},
            {"07", "14", "16", "R7"},
        )

    def test_given_fixture_rows_when_parsed_then_columns_and_text_follow_headers(self):
        plan = parse_plan(FIXTURE.read_text(encoding="utf-8"))
        rows = {row.row_id: row for row in plan.rows}
        self.assertEqual(rows["01"].text, "Paint — Pick a colour")
        self.assertEqual(rows["05"].text, "Lens — Wide lens")
        self.assertEqual(rows["05"].needs, ["01"])
        self.assertEqual(rows["06"].text, "PassedA — Done work")
        self.assertEqual(rows["07"].needs, [])
        self.assertIsNone(rows["07"].gate)
        self.assertEqual(rows["11"].needs, ["05", "L0"])
        self.assertEqual(rows["R6"].needs, ["R4", "GATE r4"])
        self.assertEqual(rows["R7"].text, "Quiet — Nothing queued")
        self.assertNotIn("99", rows)
        self.assertNotIn("F1", rows)
        self.assertNotIn("NOT-A-ROW", rows)
        self.assertNotIn("GATE-Z", [row.status for row in plan.rows])

    def test_given_words_around_a_gate_when_detected_then_last_open_id_wins(self):
        self.assertEqual(open_gate_id("GATE-A passed; GATE G4"), "G4")
        self.assertEqual(open_gate_id("GATE G4 then GATE-A passed"), "G4")
        self.assertEqual(open_gate_id("GATE G2 (built 4bd25c2)"), "G2")
        self.assertIsNone(open_gate_id("at G6"))
        self.assertIsNone(open_gate_id("gate r6"))
        self.assertIsNone(open_gate_id("GATE r6 passed"))

    def test_a_finished_row_never_opens_the_later_gate_it_mentions(self):
        self.assertIsNone(open_gate_id("DONE 99a14c3 (GATE-A passed: pt 2 is checked at GATE-L)"))
        self.assertIsNone(open_gate_id("CLOSED, never run: see GATE X"))

    def test_a_gate_without_an_id_stays_open_under_its_row(self):
        self.assertEqual(open_gate_id("step 7 GATE: a day of use", "29"), "row-29")

    def test_given_header_names_when_case_differs_then_needs_and_status_still_resolve(self):
        plan = parse_plan(
            "| # | Phase | Outcome | Needs | STATUS |\n"
            "|---|---|---|---|---|\n"
            "| 1 | Paint | Walls | — | TODO |\n"
        )
        self.assertEqual([row.row_id for row in runnable_rows(plan)], ["1"])

    def test_given_id_column_not_hash_when_parsed_then_table_is_ignored(self):
        plan = parse_plan(
            "| ID | Phase | Outcome | needs | Status |\n"
            "|---|---|---|---|---|\n"
            "| 1 | Paint | Walls | — | GATE r6 |\n"
        )
        self.assertEqual(plan.rows, [])

    def test_given_table_without_needs_when_parsed_then_it_is_ignored(self):
        plan = parse_plan(
            "| # | Phase | Status |\n"
            "|---|---|---|\n"
            "| 99 | Ghost | GATE ghost |\n"
        )
        self.assertEqual(plan.rows, [])

    def test_given_unknown_need_when_resolved_then_row_stays_runnable(self):
        plan = parse_plan(
            "| # | Phase | Outcome | needs | Status |\n"
            "|---|---|---|---|---|\n"
            "| 1 | Paint | Walls | mystery | TODO |\n"
        )
        self.assertEqual([row.row_id for row in runnable_rows(plan)], ["1"])

    def test_given_need_of_a_later_row_when_resolved_then_blocking_is_not_transitive(self):
        plan = parse_plan(
            "| # | Phase | Outcome | needs | Status |\n"
            "|---|---|---|---|---|\n"
            "| 1 | Paint | Walls | — | TODO |\n"
            "| 2 | Wait | Done | 1 | DONE |\n"
            "| 3 | Next | Go | 2 | TODO |\n"
        )
        self.assertEqual({row.row_id for row in runnable_rows(plan)}, {"1", "3"})

    def test_given_open_gate_token_when_needed_then_row_is_not_runnable(self):
        plan = parse_plan(
            "| # | Phase | Outcome | needs | Status |\n"
            "|---|---|---|---|---|\n"
            "| 1 | Paint | Walls | — | GATE L0 |\n"
            "| 2 | Next | Go | L0 | TODO |\n"
            "| 3 | Later | Go | GATE L0 | TODO |\n"
        )
        self.assertEqual(runnable_rows(plan), [])

    def test_given_sample_plan_when_scanned_then_gates_and_runnable_rows_show(self):
        dest = self.project("sample") / "docs" / "plans"
        dest.mkdir(parents=True)
        shutil.copy(FIXTURE, dest / "README.md")
        result = scan(self.config())
        gates = [item for item in result.items if item["source"] == "gate"]
        self.assertEqual(len(gates), 6)
        self.assertEqual({item["gate"] for item in gates}, {"r6", "G2", "G4", "G6", "L0"})
        self.assertEqual([item["section"] for item in gates], ["Check"] * 6)
        g4 = next(item for item in gates if item["gate"] == "G4")
        self.assertEqual(g4["text"], "Welcome — Schema flag")
        self.assertEqual(g4["kind"], "check")
        self.assertEqual(g4["where"], "phone")
        self.assertEqual(g4["id"], "sample#G4")
        runnable = {item["id"] for item in result.items if item["source"] == "runnable"}
        self.assertEqual(
            runnable,
            {"sample#row-07", "sample#row-14", "sample#row-16", "sample#row-R7"},
        )
        self.assertNotIn("sample#row-10", {item["id"] for item in result.items})
        self.assertNotIn("sample#A", {item["id"] for item in result.items})
        self.assertNotIn("sample#ghost", {item["id"] for item in result.items})
        self.assertNotIn("sample#fenced", {item["id"] for item in result.items})
        self.assertNotIn("sample#99", {item["id"] for item in result.items})
        self.assertFalse(any(item["source"] == "git" for item in result.items))
        self.assertIn("Paint — Pick a colour", result.text)
        self.assertNotIn("PassedA", result.text)
        self.assertIn("Quiet — Nothing queued", result.text)
        self.assertNotIn("fe98b2b", result.text)
        self.assertNotIn("S20U", result.text)
        self.assertNotIn("22d86ee", result.text)
        self.assertIn("snoozed 0 · backlog 0 · orphans: none", result.text)
