"""Config precedence: env > .env > agent.toml > defaults."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import os
import tempfile
import unittest

from hugpy_agent.config import (DEFAULT_AGENT_BRAIN, DEFAULT_BASE,
                                DEFAULT_MODEL, load_config)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, text):
        with open(os.path.join(self.ws, name), "w") as fh:
            fh.write(text)

    def test_defaults(self):
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws})
        self.assertEqual(cfg.base, DEFAULT_BASE)
        self.assertEqual(cfg.model, DEFAULT_MODEL)
        self.assertEqual(cfg.api_key, "")
        self.assertEqual(cfg.workspace, os.path.realpath(self.ws))

    def test_toml_lowest(self):
        self._write("agent.toml", 'model = "toml-model"\nmax_steps = 7\n')
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws})
        self.assertEqual(cfg.model, "toml-model")
        self.assertEqual(cfg.max_steps, 7)

    def test_env_file_beats_toml(self):
        self._write("agent.toml", 'model = "toml-model"\n')
        self._write(".env", "HUGPY_MODEL=envfile-model\n")
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws})
        self.assertEqual(cfg.model, "envfile-model")

    def test_environ_beats_env_file(self):
        self._write("agent.toml", 'model = "toml-model"\n')
        self._write(".env", "HUGPY_MODEL=envfile-model\n")
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws,
                                   "HUGPY_MODEL": "env-model"})
        self.assertEqual(cfg.model, "env-model")

    def test_cli_overrides_beat_environ(self):
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws,
                                   "HUGPY_MODEL": "env-model"},
                          overrides={"model": "cli-model"})
        self.assertEqual(cfg.model, "cli-model")

    def test_env_file_quotes_and_comments(self):
        self._write(".env", '# comment\nHUGPY_BASE="https://x.example/api"\n\n')
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws})
        self.assertEqual(cfg.base, "https://x.example/api")

    def test_garbage_int_ignored(self):
        self._write(".env", "HUGPY_MAX_STEPS=lots\n")
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws})
        self.assertEqual(cfg.max_steps, 25)

    def test_agent_toml_section_form(self):
        self._write("agent.toml", '[agent]\nmodel = "sectioned"\n')
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws})
        self.assertEqual(cfg.model, "sectioned")


if __name__ == "__main__":
    unittest.main()


class AgentBrainKnobTests(unittest.TestCase):
    """HUGPY_AGENT_BRAIN (2026-07-17): the dedicated agent-brain env knob.
    Same attribute as HUGPY_MODEL; the dedicated name wins when both are set."""

    def test_brain_alone_sets_model(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_AGENT_BRAIN": "brain-model"})
            self.assertEqual(cfg.model, "brain-model")

    def test_brain_beats_generic_model(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_MODEL": "generic-model",
                                       "HUGPY_AGENT_BRAIN": "brain-model"})
            self.assertEqual(cfg.model, "brain-model")

    def test_generic_model_still_works_alone(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_MODEL": "generic-model"})
            self.assertEqual(cfg.model, "generic-model")

    def test_cli_still_beats_brain(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_AGENT_BRAIN": "brain-model"},
                              overrides={"model": "cli-model"})
            self.assertEqual(cfg.model, "cli-model")
