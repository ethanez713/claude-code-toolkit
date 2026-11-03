import hashlib
import stat

from orchestrator.markers import (
    append_line,
    delete_line,
    line_blocked,
    marker_id,
    parse_marker,
    parse_worklog,
    replace_line,
    runnable_steps,
)
from support import TempCase


WORKLOG = """```
- [?] decide | anywhere | Fenced question
- [ ] Q. Fenced step
```
- [x] A. First step done
- [ ] B. Second step — needs: A
- [ ] C. Third — needs: A, B
- [ ] E. Range — needs: A-D
- [x] D. Fourth done
- [ ] F. Held back — needs: A
- [ ] G. Has GATE in it — needs: A
- [>] H. In progress and ready
- [ ] J. Comma space — needs: A, D
- [ ] K. Tight — needs: A,D
- [x] 4b. Limit landed
- [ ] 5. After the limit — needs: 4b
- [ ] L. the garden gate is fine
- [ ] M. withheld on purpose
## Open findings from the audit (reported, not fixed: leave them)
- [ ] Z. Old bug stays backlog
- [x] Y. Fixed finding
### nested
- [ ] N. Nested finding
## After
- [ ] I. After the heading is runnable
"""


class MarkerParseTest(TempCase):
    def test_given_valid_marker_when_parsed_then_fields_any_order(self):
        marker = parse_marker("- [?] check | phone | Pick one | gate: r6 | since: 2026-10-01")
        self.assertEqual(marker.kind, "check")
        self.assertEqual(marker.where, "phone")
        self.assertEqual(marker.text, "Pick one")
        self.assertEqual(marker.fields["gate"], "r6")
        self.assertEqual(marker.since, "2026-10-01")
        self.assertEqual(marker.field_order, ["gate", "since"])
        self.assertFalse(marker.malformed)

    def test_given_answered_line_when_parsed_then_answer_kept(self):
        marker = parse_marker(
            "- [a] decide | anywhere | Pick one | answer: yes | answered: 2026-11-01"
        )
        self.assertEqual(marker.mark, "a")
        self.assertEqual(marker.fields["answer"], "yes")
        self.assertEqual(marker.fields["answered"], "2026-11-01")

    def test_given_malformed_marker_when_parsed_then_kind_is_question_mark(self):
        marker = parse_marker("- [?] this is not split")
        self.assertTrue(marker.malformed)
        self.assertEqual(marker.kind, "?")
        self.assertEqual(marker.text, "this is not split")

    def test_given_text_when_id_built_then_sha1_prefix_is_stable(self):
        digest = hashlib.sha1(b"Pick one").hexdigest()[:4]
        self.assertEqual(marker_id("sample", "Pick one"), f"sample#{digest}")
        self.assertEqual(marker_id("sample", "Pick one"), marker_id("sample", "Pick one"))

    def test_given_gate_and_held_words_when_checked_then_only_those_block(self):
        self.assertTrue(line_blocked("Has GATE in it"))
        self.assertFalse(line_blocked("the garden gate is fine"))
        self.assertTrue(line_blocked("Held back"))
        self.assertTrue(line_blocked("held"))
        self.assertFalse(line_blocked("withheld on purpose"))

    def test_given_worklog_when_steps_resolved_then_ready_set_and_backlog(self):
        parsed = parse_worklog(WORKLOG)
        ready = {step.step_id for step in runnable_steps(parsed.steps)}
        self.assertEqual(ready, {"B", "H", "J", "K", "5", "I", "L", "M"})
        self.assertEqual(parsed.backlog, 2)
        self.assertEqual(
            [step.step_id for step in parsed.steps if step.backlog and step.mark != "x"],
            ["Z", "N"],
        )
        self.assertEqual(parsed.markers, [])

    def test_given_finished_letter_range_when_needed_then_step_is_runnable(self):
        parsed = parse_worklog(
            "- [x] A. a\n- [x] B. b\n- [x] C. c\n- [x] D. d\n- [ ] E. Range — needs: A-D\n"
        )
        self.assertEqual([step.step_id for step in runnable_steps(parsed.steps)], ["E"])

    def test_given_step_id_that_looks_like_a_range_when_needed_then_it_is_not_expanded(self):
        parsed = parse_worklog("- [x] A-D. named\n- [ ] B. next — needs: A-D\n")
        self.assertEqual([step.step_id for step in runnable_steps(parsed.steps)], ["B"])

    def test_given_finished_number_range_when_needed_then_step_is_runnable(self):
        parsed = parse_worklog(
            "- [x] 1. one\n- [x] 2. two\n- [x] 3. three\n- [ ] 4. span — needs: 1-3\n"
        )
        self.assertEqual([step.step_id for step in runnable_steps(parsed.steps)], ["4"])

    def test_given_missing_need_when_resolved_then_step_is_not_runnable(self):
        parsed = parse_worklog("- [ ] A. Ready — needs: missing\n")
        self.assertEqual(runnable_steps(parsed.steps), [])

    def test_given_open_and_done_same_id_when_needed_then_need_is_unsatisfied(self):
        parsed = parse_worklog("- [x] A. Done\n- [ ] A. Open again\n- [ ] B. After — needs: A\n")
        ready = [step.step_id for step in runnable_steps(parsed.steps)]
        self.assertEqual(ready, ["A"])

    def test_given_findings_heading_when_parsed_then_open_steps_are_backlog(self):
        parsed = parse_worklog("## FINDINGS\n- [ ] Z. Stay\n")
        self.assertEqual(parsed.backlog, 1)
        self.assertEqual(runnable_steps(parsed.steps), [])

    def test_given_reported_heading_when_parsed_then_open_steps_are_backlog(self):
        parsed = parse_worklog("## Reported, not fixed items\n- [ ] Z. Stay\n")
        self.assertEqual(parsed.backlog, 1)
        self.assertEqual(runnable_steps(parsed.steps), [])


class MarkerRewriteTest(TempCase):
    def test_given_other_lines_when_one_is_replaced_then_the_rest_stay_byte_identical(self):
        path = self.worklogs / "sample.md"
        body = b"keep me  \r\n- [?] decide | anywhere | Pick one\nkeep2  \n"
        path.write_bytes(body)
        path.chmod(0o640)
        replaced = replace_line(
            path,
            "- [?] decide | anywhere | Pick one",
            "- [a] decide | anywhere | Pick one | answer: yes",
        )
        self.assertTrue(replaced)
        self.assertEqual(
            path.read_bytes(),
            b"keep me  \r\n- [a] decide | anywhere | Pick one | answer: yes\nkeep2  \n",
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_given_other_lines_when_one_is_deleted_then_the_rest_stay_byte_identical(self):
        path = self.worklogs / "sample.md"
        path.write_bytes(b"keep  \r\n- [?] decide | anywhere | Pick one\nkeep2  \n")
        self.assertTrue(delete_line(path, "- [?] decide | anywhere | Pick one"))
        self.assertEqual(path.read_bytes(), b"keep  \r\nkeep2  \n")

    def test_given_missing_line_when_replaced_then_file_is_unchanged(self):
        path = self.worklogs / "sample.md"
        body = b"leave me\n"
        path.write_bytes(body)
        self.assertFalse(replace_line(path, "absent", "nope"))
        self.assertEqual(path.read_bytes(), body)

    def test_given_existing_file_when_line_appended_then_prefix_and_mode_stay(self):
        path = self.worklogs / "sample.md"
        path.write_bytes(b"hello  \n")
        path.chmod(0o640)
        append_line(path, "- [?] decide | anywhere | Pick one | since: 2026-11-02")
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"hello  \n"))
        self.assertTrue(data.endswith(b"- [?] decide | anywhere | Pick one | since: 2026-11-02\n"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_given_new_file_when_line_appended_then_mode_is_private(self):
        path = self.worklogs / "fresh" / "sample.md"
        append_line(path, "- [?] decide | anywhere | Pick one")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))


if __name__ == "__main__":
    import unittest
    unittest.main()
