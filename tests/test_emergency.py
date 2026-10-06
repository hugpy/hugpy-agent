import _bootstrap  # noqa: F401
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hugpy_agent import emergency as em


def _touch(path, size=0):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_find_llama_bin_env_override_and_executable_check(self):
        binp = os.path.join(self.d, "llama-server")
        _touch(binp)
        os.environ["HUGPY_EMERGENCY_LLAMA_BIN"] = binp
        # isolate from any real binary installed on this box
        with mock.patch.object(em, "_LLAMA_BIN_CANDIDATES", ()), \
             mock.patch.object(em.shutil, "which", lambda _n: None):
            self.assertEqual(em.find_llama_bin(), "")      # not executable yet
            os.chmod(binp, 0o755)
            self.assertEqual(em.find_llama_bin(), binp)

    def test_list_models_filters_and_sorts(self):
        root = os.path.join(self.d, "gguf")
        _touch(os.path.join(root, "Qwen3-Instruct-Q4.gguf"), 3000)
        _touch(os.path.join(root, "big-base.gguf"), 9000)                 # chat-ish? no hint -> second tier
        _touch(os.path.join(root, "clip-encoder.gguf"), 100)              # non-chat hint -> skipped
        _touch(os.path.join(root, "model.mmproj.gguf"), 100)             # mmproj -> skipped
        _touch(os.path.join(root, "shard-00002-of-00004.gguf"), 100)    # non-first shard -> skipped
        _touch(os.path.join(root, "shard-00001-of-00004.gguf"), 5000)   # first shard -> kept
        os.environ["HUGPY_EMERGENCY_MODEL_DIRS"] = root
        with mock.patch.object(em, "_MIN_MODEL_BYTES", 0):
            names = [m.name for m in em.list_models()]
        self.assertIn("Qwen3-Instruct-Q4.gguf", names)
        self.assertIn("shard-00001-of-00004.gguf", names)
        self.assertNotIn("clip-encoder.gguf", names)
        self.assertNotIn("model.mmproj.gguf", names)
        self.assertNotIn("shard-00002-of-00004.gguf", names)
        # chat-hint ("qwen") sorts before a no-hint model
        self.assertEqual(names[0], "Qwen3-Instruct-Q4.gguf")

    def test_mmproj_detected_beside_model(self):
        root = os.path.join(self.d, "v")
        _touch(os.path.join(root, "vlm-chat.gguf"), 2000)
        _touch(os.path.join(root, "vlm.mmproj.gguf"), 50)
        os.environ["HUGPY_EMERGENCY_MODEL_DIRS"] = root
        with mock.patch.object(em, "_MIN_MODEL_BYTES", 0):
            models = em.list_models()
        self.assertEqual(len(models), 1)
        self.assertTrue(models[0].mmproj.endswith("vlm.mmproj.gguf"))

    def test_imatrix_and_tiny_files_excluded(self):
        root = os.path.join(self.d, "g")
        _touch(os.path.join(root, "Model-Q4.imatrix.gguf"), 30 << 20)  # imatrix -> skip
        _touch(os.path.join(root, "tiny-chat.gguf"), 1000)             # under floor -> skip
        _touch(os.path.join(root, "Qwen-real.gguf"), 30 << 20)         # kept
        os.environ["HUGPY_EMERGENCY_MODEL_DIRS"] = root
        self.assertEqual([m.name for m in em.list_models()], ["Qwen-real.gguf"])

    def test_discovery_never_raises_on_bad_root(self):
        os.environ["HUGPY_EMERGENCY_MODEL_DIRS"] = "/nope/does/not/exist"
        self.assertEqual(em.list_models(), [])

    def test_preflight_reports_errors(self):
        os.environ["HUGPY_EMERGENCY_LLAMA_BIN"] = ""
        os.environ["HUGPY_EMERGENCY_MODEL_DIRS"] = "/nope"
        with mock.patch.object(em, "_LLAMA_BIN_CANDIDATES", ()), \
             mock.patch.object(em.shutil, "which", lambda _n: None):
            pf = em.preflight()
        self.assertFalse(pf.ready)
        self.assertTrue(any("llama-server" in e for e in pf.errors))
        self.assertTrue(any("GGUF" in e for e in pf.errors))

    def test_default_threads_and_free_port(self):
        self.assertGreaterEqual(em.default_threads(), 1)
        p = em.free_port()
        self.assertTrue(8900 <= p < 9099)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _model(self, name="m.gguf", mmproj=""):
        p = os.path.join(self.d, name)
        _touch(p, 1000)
        return em.Model(path=p, name=name, size_bytes=1000, mmproj=mmproj)

    def test_argv_and_base_url(self):
        m = self._model(mmproj="/x/proj.gguf")
        s = em.EmergencyServer(m, llama_bin="/bin/true", port=8950, threads=3,
                               ctx=4096, gpu_layers="cpu")
        argv = s._argv()
        self.assertIn("--mmproj", argv)
        self.assertIn("-ngl", argv)
        self.assertEqual(argv[argv.index("-ngl") + 1], "0")
        self.assertEqual(argv[argv.index("-c") + 1], "4096")
        self.assertEqual(s.base_url, "http://127.0.0.1:8950/v1")

    def test_argv_auto_gpu_omits_ngl(self):
        s = em.EmergencyServer(self._model(), llama_bin="/bin/true", port=8951,
                               gpu_layers="auto")
        self.assertNotIn("-ngl", s._argv())

    def test_profile_is_openai_chat_pointing_local(self):
        s = em.EmergencyServer(self._model("Qwen.gguf"), llama_bin="/bin/true", port=8952)
        prof = s.profile()
        self.assertEqual(prof["protocol"], "openai-chat")
        self.assertEqual(prof["base_url"], "http://127.0.0.1:8952/v1")
        self.assertEqual(prof["model"], "Qwen.gguf")
        self.assertTrue(prof["emergency"])
        self.assertLess(prof["max_tokens"], prof["context_length"])

    def test_start_errors_on_missing_bin_and_model(self):
        with mock.patch.object(em, "find_llama_bin", lambda: ""):
            with self.assertRaises(RuntimeError):
                em.EmergencyServer(self._model(), llama_bin="").start()
        gone = em.Model(path="/no/such.gguf", name="x", size_bytes=0)
        with self.assertRaises(RuntimeError):
            em.EmergencyServer(gone, llama_bin="/bin/true", port=8953).start()

    def test_wait_ready_detects_crash_without_hanging(self):
        # a "binary" that exits immediately -> wait_ready returns fast, ok False
        fake = os.path.join(self.d, "crasher.sh")
        with open(fake, "w") as f:
            f.write("#!/bin/bash\nexit 7\n")
        os.chmod(fake, 0o755)
        s = em.EmergencyServer(self._model(), llama_bin=fake, port=em.free_port(),
                               log_dir=self.d).start()
        ok, detail = s.wait_ready(timeout=10)
        self.assertFalse(ok)
        self.assertIn("exited", detail)
        s.stop()


if __name__ == "__main__":
    unittest.main()
