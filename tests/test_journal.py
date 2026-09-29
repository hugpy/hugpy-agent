"""Journal ledger semantics: WAL persistence, idempotency, compaction wire view."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import os
import tempfile
import unittest

from hugpy_agent.journal import Journal, idem_key


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "j.db")
        self.j = Journal(self.db)

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def test_run_and_messages_survive_reopen(self):
        rid = self.j.create_run("task", "m")
        self.j.append_message(rid, "system", "sys")
        self.j.append_message(rid, "user", "hello")
        self.j.close()
        j2 = Journal(self.db)   # simulates a fresh process after a kill
        msgs = j2.wire_messages(rid)
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertEqual(j2.get_run(rid)["status"], "running")
        j2.close()

    def test_idem_key_stable_and_distinct(self):
        a = idem_key("r", 3, "shell", {"command": "ls", "timeout": 5})
        b = idem_key("r", 3, "shell", {"timeout": 5, "command": "ls"})
        c = idem_key("r", 4, "shell", {"command": "ls", "timeout": 5})
        self.assertEqual(a, b)       # arg order canonicalized
        self.assertNotEqual(a, c)    # different requesting message

    def test_record_before_execute_protocol(self):
        rid = self.j.create_run("t", "m")
        key = idem_key(rid, 2, "shell", {"command": "ls"})
        self.j.record_call_start(key, rid, 2, "shell", {"command": "ls"})
        row = self.j.lookup_call(key)
        self.assertEqual(row["status"], "pending")
        self.j.record_call_result(key, "done", "output")
        row = self.j.lookup_call(key)
        self.assertEqual((row["status"], row["result"]), ("done", "output"))

    def test_duplicate_start_is_ignored_not_reset(self):
        rid = self.j.create_run("t", "m")
        key = idem_key(rid, 2, "x", {})
        self.j.record_call_start(key, rid, 2, "x", {})
        self.j.record_call_result(key, "done", "res")
        self.j.record_call_start(key, rid, 2, "x", {})   # resume re-entry
        self.assertEqual(self.j.lookup_call(key)["status"], "done")

    def test_wire_messages_compaction(self):
        rid = self.j.create_run("t", "m")
        self.j.append_message(rid, "system", "sys")      # seq 0 (pinned)
        self.j.append_message(rid, "user", "brief")      # seq 1 (pinned)
        for i in range(4):                               # seq 2..5
            self.j.append_message(rid, "assistant", "old-%d" % i)
        self.j.append_message(rid, "user", "recent")     # seq 6
        self.j.append_message(rid, "summary", "the summary",
                              meta={"replaces_upto": 5})
        wire = self.j.wire_messages(rid)
        contents = [m["content"] for m in wire]
        self.assertEqual(contents[0], "sys")
        self.assertEqual(contents[1], "brief")
        self.assertIn("the summary", contents[2])
        self.assertEqual(contents[3], "recent")
        self.assertEqual(len(wire), 4)                   # old-0..3 replaced

    def test_kv(self):
        self.assertIsNone(self.j.kv_get("k"))
        self.j.kv_set("k", "1")
        self.j.kv_set("k", "2")
        self.assertEqual(self.j.kv_get("k"), "2")


if __name__ == "__main__":
    unittest.main()
