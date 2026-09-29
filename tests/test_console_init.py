"""launch_claude_code init-prompt on call: an explicit init_prompt or
$HUGPY_INIT_PROMPT is passed to Claude Code as --append-system-prompt; empty
leaves the bare exec unchanged (sparing). This is the general init-prompt hook
a caller (e.g. the console handing down the Steward's reach) uses at launch."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import os
import unittest

from hugpy_agent import console


class InitPromptTests(unittest.TestCase):
    def setUp(self):
        self._exec, self._rebind, self._models = (
            os.execvp, console._rebind_stdin_to_tty, console.models_url)
        # launch_claude_code mutates os.environ (ANTHROPIC_BASE_URL/AUTH_TOKEN)
        # before the exec we stub out — snapshot and restore the WHOLE env so
        # nothing leaks into later tests or their subprocesses.
        self._env = dict(os.environ)
        self.cap = {}
        console._rebind_stdin_to_tty = lambda: None
        console.models_url = lambda c: "https://x/api/v1/models"

        def fake_exec(binary, argv):
            self.cap["argv"] = argv
            raise SystemExit
        os.execvp = fake_exec
        os.environ.pop("HUGPY_INIT_PROMPT", None)

    def tearDown(self):
        os.execvp, console._rebind_stdin_to_tty, console.models_url = (
            self._exec, self._rebind, self._models)
        os.environ.clear()
        os.environ.update(self._env)

    def _launch(self, **kw):
        try:
            console.launch_claude_code("https://x", "k", binary="claude", **kw)
        except SystemExit:
            pass
        return self.cap["argv"]

    def test_on_call_init_prompt(self):
        self.assertEqual(self._launch(init_prompt="CAGE"),
                         ["claude", "--append-system-prompt", "CAGE"])

    def test_env_init_prompt(self):
        os.environ["HUGPY_INIT_PROMPT"] = "ENV CAGE"
        self.assertEqual(self._launch(),
                         ["claude", "--append-system-prompt", "ENV CAGE"])

    def test_explicit_arg_overrides_env(self):
        os.environ["HUGPY_INIT_PROMPT"] = "ENV"
        self.assertEqual(self._launch(init_prompt="ARG"),
                         ["claude", "--append-system-prompt", "ARG"])

    def test_empty_is_bare_exec(self):
        self.assertEqual(self._launch(), ["claude"])


if __name__ == "__main__":
    unittest.main()
