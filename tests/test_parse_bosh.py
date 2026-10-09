import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parse_bosh import (  # noqa: E402
    DebugFilter,
    classify_stage_family,
    filter_debug,
    is_level_line,
    parse_events,
    parse_ruby_log_line,
)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def read_lines(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.readlines()


class TestParseEvents(unittest.TestCase):
    def setUp(self):
        self.lines = read_lines("task-84098.events.jsonl")
        self.snapshot = parse_events(self.lines, now=1791555570)

    def test_creates_missing_vms_chore_duration(self):
        chores = self.snapshot["chores"]
        creating = [c for c in chores if c["stage"] == "Creating missing vms"]
        self.assertEqual(len(creating), 1)
        chore = creating[0]
        self.assertEqual(chore["state"], "finished")
        self.assertEqual(chore["duration_ms"], 56000)
        self.assertAlmostEqual(chore["duration_ms"] / 1000.0, 56, delta=1)

    def test_nested_starting_jobs_sub_chore(self):
        chores = self.snapshot["chores"]
        updating = [c for c in chores if c["stage"] == "Updating instance"]
        self.assertEqual(len(updating), 1)
        parent = updating[0]
        self.assertEqual(parent["state"], "finished")
        sub_names = [s["name"] for s in parent["sub_chores"]]
        self.assertIn("starting jobs", sub_names)
        starting = next(s for s in parent["sub_chores"] if s["name"] == "starting jobs")
        self.assertIsNotNone(starting["end"])
        self.assertGreater(starting["duration_ms"], 0)

    def test_drain_and_stop_sub_chores_present(self):
        chores = self.snapshot["chores"]
        parent = next(c for c in chores if c["stage"] == "Updating instance")
        sub_names = [s["name"] for s in parent["sub_chores"]]
        self.assertIn("executing drain", sub_names)
        self.assertIn("stopping jobs", sub_names)

    def test_warnings_attach_to_open_chore(self):
        chores = self.snapshot["chores"]
        preparing = next(c for c in chores if c["stage"] == "Preparing deployment" and c["task"] == "Preparing deployment")
        messages = [w["message"] for w in preparing["warnings"]]
        self.assertTrue(any("DNS address not available" in m for m in messages))

    def test_stage_family_classification(self):
        self.assertEqual(classify_stage_family("Creating missing vms"), "create_vm")
        self.assertEqual(classify_stage_family("Updating instance"), "update_instance")
        self.assertEqual(classify_stage_family("Preparing deployment"), "prepare")
        self.assertEqual(classify_stage_family("Preparing package compilation"), "compile")

    def test_all_chores_closed_in_finished_fixture(self):
        for chore in self.snapshot["chores"]:
            self.assertIn(chore["state"], ("finished", "error"))
            self.assertIsNotNone(chore["end"])


class TestLiveOpenChore(unittest.TestCase):
    def test_open_chore_duration_grows_with_now(self):
        lines = [
            '{"time":1000,"stage":"Updating instance","tags":[],"task":"x/0","index":1,"state":"started","progress":0}',
        ]
        snap_early = parse_events(lines, now=1005)
        snap_later = parse_events(lines, now=1050)
        chore_early = snap_early["chores"][0]
        chore_later = snap_later["chores"][0]
        self.assertIsNone(chore_early["end"])
        self.assertEqual(chore_early["duration_ms"], 5000)
        self.assertEqual(chore_later["duration_ms"], 50000)


class TestDebugFilter(unittest.TestCase):
    def test_fixture_keeps_info_warn_error_and_task_lines(self):
        lines = read_lines("task-84098.debug.redacted.log")
        result = filter_debug(lines)
        self.assertGreater(len(result["lines"]), 0)
        levels = {entry["level"] for entry in result["lines"]}
        self.assertTrue(levels.issubset({"INFO", "WARN", "ERROR", "FATAL"}))

    def test_fixture_captures_warnings_and_errors(self):
        lines = read_lines("task-84098.debug.redacted.log")
        result = filter_debug(lines)
        self.assertGreater(len(result["warnings"]), 0)
        self.assertGreater(len(result["errors"]), 0)
        self.assertTrue(any("DNS address not available" in w["message"] for w in result["warnings"]))

    def test_ruby_log_line_parses_ts_level_logger_message(self):
        line = "I, [2026-10-09T14:16:12.794823 #29] [0xaa4a80]  INFO -- TaskHelper: Director Version: 283.1.0"
        parsed = parse_ruby_log_line(line)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["ts"], "2026-10-09T14:16:12.794823")
        self.assertEqual(parsed["level"], "INFO")
        self.assertEqual(parsed["logger"], "TaskHelper")
        self.assertEqual(parsed["message"], "Director Version: 283.1.0")

    def test_debug_level_line_is_dropped(self):
        line = "D, [2026-10-09T14:16:12.000000 #29] [0xaa4a80] DEBUG -- TaskHelper: some chatter"
        self.assertFalse(is_level_line(line))
        f = DebugFilter()
        entry = f.feed_line(line)
        self.assertIsNone(entry)

    def test_sql_debug_line_is_dropped(self):
        line = ("D, [2026-10-09T14:16:12.000000 #29] [0xaa4a80] DEBUG -- "
                "ActiveRecord: SELECT * FROM tasks WHERE id = 1")
        f = DebugFilter()
        entry = f.feed_line(line)
        self.assertIsNone(entry)
        snap = filter_debug([line])
        self.assertEqual(snap["lines"], [])

    def test_pem_block_is_redacted(self):
        lines = [
            "I, [2026-10-09T14:16:12.000000 #29] [] INFO -- TaskHelper: before key",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAA",
            "-----END OPENSSH PRIVATE KEY-----",
            "I, [2026-10-09T14:16:12.000000 #29] [] INFO -- TaskHelper: after key",
        ]
        result = filter_debug(lines)
        raws = [e["raw"] for e in result["lines"]]
        self.assertIn("[REDACTED PEM]", raws)
        joined = "\n".join(raws)
        self.assertNotIn("BEGIN OPENSSH", joined)
        self.assertNotIn("b3BlbnNzaC1rZXktdjEA", joined)
        self.assertTrue(any("before key" in r for r in raws))
        self.assertTrue(any("after key" in r for r in raws))

    def test_manifest_dump_is_dropped_until_next_level_line(self):
        lines = [
            "I, [2026-10-09T14:16:12.000000 #29] [] INFO -- TaskHelper: Manifest:",
            "name: service-instance",
            "  releases:",
            "  - name: postgres",
            "I, [2026-10-09T14:16:13.000000 #29] [] INFO -- TaskHelper: Reading deployment manifest",
        ]
        result = filter_debug(lines)
        messages = [e["message"] for e in result["lines"]]
        self.assertFalse(any("releases" in m for m in messages))
        self.assertFalse(any(m == "name: service-instance" for m in messages))
        self.assertTrue(any("Reading deployment manifest" in m for m in messages))

    def test_password_value_is_redacted(self):
        line = ("I, [2026-10-09T14:16:12.000000 #29] [] INFO -- TaskHelper: "
                "config loaded password: hunter2 ok")
        result = filter_debug([line])
        message = result["lines"][0]["message"]
        self.assertNotIn("hunter2", message)
        self.assertIn("[REDACTED]", message)

    def test_private_key_value_is_redacted(self):
        line = ('I, [2026-10-09T14:16:12.000000 #29] [] INFO -- TaskHelper: '
                'creds={"private_key"=>"supersecretvalue"}')
        result = filter_debug([line])
        message = result["lines"][0]["message"]
        self.assertNotIn("supersecretvalue", message)


if __name__ == "__main__":
    unittest.main()
