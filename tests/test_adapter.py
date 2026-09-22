"""Adapter parse/repair/validation cases — the sloppy-small-model gauntlet."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import unittest

from hugpy_agent.adapter import (Adapter, MODE_CONSTRAINED, MODE_NATIVE, scrub)
from hugpy_agent.gateway import CONTINUATION_LEAK


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.ad = Adapter()

    def test_single_block(self):
        out = self.ad.extract(
            'Let me look.\n<tool_call>\n{"name": "fs_read", '
            '"arguments": {"path": "a.txt"}}\n</tool_call>')
        self.assertEqual(len(out.calls), 1)
        self.assertEqual(out.calls[0].name, "fs_read")
        self.assertEqual(out.calls[0].arguments, {"path": "a.txt"})
        self.assertEqual(out.plain_text, "Let me look.")
        self.assertEqual(out.errors, [])

    def test_multiple_blocks_all_parsed(self):
        text = ('<tool_call>{"name": "a", "arguments": {}}</tool_call>\n'
                '<tool_call>{"name": "b", "arguments": {"x": 1}}</tool_call>')
        out = self.ad.extract(text)
        self.assertEqual([c.name for c in out.calls], ["a", "b"])

    def test_malformed_json_reports_error(self):
        out = self.ad.extract(
            '<tool_call>{"name": "fs_read", "arguments": {"path": }</tool_call>')
        self.assertEqual(out.calls, [])
        self.assertEqual(len(out.errors), 1)
        self.assertIn("not valid JSON", out.errors[0])

    def test_missing_name_reports_error(self):
        out = self.ad.extract('<tool_call>{"arguments": {}}</tool_call>')
        self.assertEqual(out.calls, [])
        self.assertIn("'name'", out.errors[0])

    def test_double_encoded_arguments_unwrapped(self):
        out = self.ad.extract(
            '<tool_call>{"name": "fs_read", "arguments": '
            '"{\\"path\\": \\"a.txt\\"}"}</tool_call>')
        self.assertEqual(out.calls[0].arguments, {"path": "a.txt"})

    def test_bare_json_call_tolerated(self):
        """A sloppy model dropping the XML tags must still be understood."""
        out = self.ad.extract(
            'I will read it: {"name": "fs_read", "arguments": {"path": "a.txt"}}')
        self.assertEqual(len(out.calls), 1)
        self.assertEqual(out.calls[0].name, "fs_read")

    def test_bare_json_nested_braces(self):
        out = self.ad.extract(
            '{"name": "fs_write", "arguments": {"path": "r.md", '
            '"content": "x {y} z"}}')
        self.assertEqual(out.calls[0].arguments["content"], "x {y} z")

    def test_plain_prose_yields_nothing(self):
        out = self.ad.extract("The project seems to be a web server.")
        self.assertEqual(out.calls, [])
        self.assertEqual(out.errors, [])

    def test_final_answer_shorthand_string(self):
        """{"final_answer": "..."} — the envelope-less shorthand small models
        fall into; intent is unambiguous so it must parse, not abort."""
        out = self.ad.extract('{"final_answer": "debs: 1.0.46, 1.0.47"}')
        self.assertEqual(out.calls[0].name, "final_answer")
        self.assertEqual(out.calls[0].arguments, {"answer": "debs: 1.0.46, 1.0.47"})

    def test_final_answer_shorthand_nested(self):
        out = self.ad.extract(
            'Done. {"final_answer": {"answer": "all four ported"}}')
        self.assertEqual(out.calls[0].name, "final_answer")
        self.assertEqual(out.calls[0].arguments, {"answer": "all four ported"})

    def test_final_answer_marker_line(self):
        """`final_answer\\n<prose>` — explicit termination signal with a
        degraded envelope; honored. Bare prose (above) stays rejected."""
        out = self.ad.extract(
            "final_answer\nThe .deb files are 1.0.46, 1.0.47 and 1.0.48.")
        self.assertEqual(out.calls[0].name, "final_answer")
        self.assertEqual(
            out.calls[0].arguments["answer"],
            "The .deb files are 1.0.46, 1.0.47 and 1.0.48.")

    def test_final_answer_marker_requires_leading_position(self):
        """Merely MENTIONING final_answer mid-prose is not a termination
        signal."""
        out = self.ad.extract(
            "I should call final_answer once I have listed the files.")
        self.assertEqual(out.calls, [])
        self.assertEqual(out.errors, [])


class LeakTests(unittest.TestCase):
    def test_scrub_removes_leak(self):
        self.assertEqual(scrub("before " + CONTINUATION_LEAK + " after"),
                         "before  after")

    def test_leak_in_prose_does_not_break_parse(self):
        ad = Adapter()
        text = (CONTINUATION_LEAK +
                '\n<tool_call>{"name": "fs_glob", "arguments": '
                '{"pattern": "*.py"}}</tool_call>')
        out = ad.extract(text)
        self.assertEqual(out.calls[0].name, "fs_glob")
        self.assertNotIn(CONTINUATION_LEAK, out.plain_text)

    def test_leak_inside_json_block_scrubbed_before_parse(self):
        """The leak lands mid-stream, so it can split a JSON body; scrubbing
        must happen before parsing or the block is unrecoverable."""
        ad = Adapter()
        block = ('{"name": "fs_read", ' + CONTINUATION_LEAK +
                 ' "arguments": {"path": "a.txt"}}')
        out = ad.extract("<tool_call>%s</tool_call>" % block)
        self.assertEqual(len(out.calls), 1, out.errors)
        self.assertEqual(out.calls[0].arguments, {"path": "a.txt"})


class ValidateTests(unittest.TestCase):
    SCHEMA = {"type": "object",
              "properties": {"path": {"type": "string"},
                             "offset": {"type": "integer"},
                             "deep": {"type": "boolean"},
                             "mode": {"type": "string", "enum": ["r", "w"]}},
              "required": ["path"]}

    def setUp(self):
        self.ad = Adapter()

    def test_ok(self):
        errs, norm = self.ad.validate(self.SCHEMA, {"path": "a", "offset": 3})
        self.assertEqual(errs, [])
        self.assertEqual(norm["offset"], 3)

    def test_missing_required(self):
        errs, _ = self.ad.validate(self.SCHEMA, {"offset": 1})
        self.assertTrue(any("required" in e and "path" in e for e in errs))

    def test_type_mismatch(self):
        errs, _ = self.ad.validate(self.SCHEMA, {"path": 42})
        self.assertTrue(any("string" in e for e in errs))

    def test_string_number_coerced(self):
        """Small models quote numbers constantly; coercion saves a round-trip."""
        errs, norm = self.ad.validate(self.SCHEMA, {"path": "a", "offset": "128"})
        self.assertEqual(errs, [])
        self.assertEqual(norm["offset"], 128)

    def test_string_bool_coerced(self):
        errs, norm = self.ad.validate(self.SCHEMA, {"path": "a", "deep": "true"})
        self.assertEqual(errs, [])
        self.assertIs(norm["deep"], True)

    def test_enum_enforced(self):
        errs, _ = self.ad.validate(self.SCHEMA, {"path": "a", "mode": "x"})
        self.assertTrue(any("one of" in e for e in errs))

    def test_repair_message_names_the_problem(self):
        msg = self.ad.repair_message(["missing required argument 'path'"])
        self.assertEqual(msg["role"], "user")
        self.assertIn("missing required argument 'path'", msg["content"])
        self.assertIn("<tool_call>", msg["content"])


class ModeTests(unittest.TestCase):
    class _Spec:
        name = "t"
        description = "d"
        parameters = {"type": "object", "properties": {}, "required": []}

    def test_prompted_prompt_carries_schema(self):
        ad = Adapter()
        block = ad.system_prompt_block([self._Spec()])
        self.assertIn("<tools>", block)
        self.assertIn('"name":"t"', block.replace('"name": "t"', '"name":"t"'))
        self.assertIsNone(ad.wire_tools([self._Spec()]))

    def test_native_uses_wire_tools(self):
        ad = Adapter(MODE_NATIVE)
        tools = ad.wire_tools([self._Spec()])
        self.assertEqual(tools[0]["function"]["name"], "t")
        out = ad.extract("", [{"function": {"name": "t", "arguments": "{}"}}])
        self.assertEqual(out.calls[0].name, "t")

    def test_constrained_parses_bare_json(self):
        ad = Adapter(MODE_CONSTRAINED)
        out = ad.extract('{"name": "t", "arguments": {}}')
        self.assertEqual(out.calls[0].name, "t")
        resp = ad.tool_response_message("t", "ok")
        self.assertIn("action_result", resp["content"])

    def test_tool_response_prompted_format(self):
        ad = Adapter()
        msg = ad.tool_response_message("fs_read", "contents")
        self.assertEqual(msg["role"], "user")
        self.assertIn("<tool_response>", msg["content"])
        self.assertIn("contents", msg["content"])


if __name__ == "__main__":
    unittest.main()
