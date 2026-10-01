"""hugpy-agent registers Hugpy among the providers of abstract-serve-core's one
Serve console, and `serve --console` runs that console with Hugpy first."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import os
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from hugpy_agent import cli


class HugpyIsAServeProvider(unittest.TestCase):
    def test_registered_in_the_serve_provider_group(self):
        meta = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]
        self.assertEqual(meta["entry-points"]["abstract_serve.providers"],
                         {"hugpy": "abstract_serve.providers:hugpy"})
        self.assertEqual(meta["optional-dependencies"]["serve"], ["abstract-serve-core>=0.1.10"])

    def test_serve_console_runs_the_shared_console_with_hugpy_first(self):
        try:
            import abstract_serve.serve_cli  # noqa: F401  (hugpy-agent[serve])
        except ImportError:
            self.skipTest("abstract-serve-core not installed")
        with patch("abstract_serve.serve_cli.main", return_value=0) as serve_main, \
                patch.dict(os.environ, {}, clear=False):
            self.assertEqual(cli.main(["serve", "--console", "--no-browser"]), 0)
            self.assertEqual(os.environ["AC_SERVE_BACKEND"], "hugpy")
        serve_main.assert_called_once_with(["--host", "127.0.0.1", "--port", "9124", "--no-browser"])


if __name__ == "__main__":
    unittest.main()
