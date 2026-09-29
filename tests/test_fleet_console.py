import _bootstrap  # noqa: F401
import copy
import io
import unittest
from unittest.mock import Mock, patch

from hugpy_agent import fleet_console as fc
from hugpy_agent import cli


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.worker = {"id": "w", "name": "gpu", "status": "online", "admission": "approved",
                       "loaded_models": [], "models_local": ["m"], "models": ["m"]}
        self.state = {"workers": [self.worker], "catalog": [{"id": "m", "task": "text-generation"}],
                      "metrics": [], "queue": [], "errors": {}, "observed_at": 123}
        self.placement = {"workers": [{"id": "w", "name": "gpu", "already_has": True,
                                      "disk_ok": True, "fits_free_vram": True, "fits_total_vram": True}]}
        self.client = Mock()
        self.client.request.side_effect = lambda *args: copy.deepcopy(self.placement)

    def candidate(self):
        return fc.inspect_model(self.client, self.state, "m")["candidates"][0]

    def test_free_vram_is_not_loaded(self):
        row = self.candidate()
        self.assertFalse(row["hot"])
        self.assertEqual(row["readiness"], "load into free VRAM")
        self.assertIsNone(row["inference_start_eta_s"])

    def test_eviction_is_not_immediate(self):
        self.placement["workers"][0]["fits_free_vram"] = False
        row = self.candidate()
        self.assertEqual(row["readiness"], "eviction needed")
        self.assertFalse(row["callable_now"])
        self.assertTrue(row["callable_at_max"])
        self.assertIsNone(row["inference_start_eta_s"])

    def test_busy_hot_model_has_unknown_wait(self):
        self.worker["loaded_models"] = ["m"]
        self.state["queue"] = [{"model_key": "another", "state": "active"}]
        row = self.candidate()
        self.assertEqual(row["load_estimate_s"], 0)
        self.assertIsNone(row["inference_start_eta_s"])

    def test_idle_hot_model_is_ready(self):
        self.worker["loaded_models"] = ["m"]
        self.assertEqual(self.candidate()["inference_start_eta_s"], 0)

    def test_offline_and_unapproved_never_ready(self):
        self.worker["loaded_models"] = ["m"]
        for change in ({"status": "offline"}, {"admission": "pending"}, {"unreachable": True}):
            with self.subTest(change=change):
                original = dict(self.worker)
                self.worker.update(change)
                self.assertEqual(self.candidate()["readiness"], "unavailable")
                self.worker.clear()
                self.worker.update(original)

    def test_legacy_feasible_true_is_not_capacity(self):
        self.placement["workers"][0] = {"id": "w", "feasible": True, "gpu_resident": True}
        self.assertEqual(self.candidate()["readiness"], "capacity unknown")

    def test_unhealthy_allocation_not_hot(self):
        self.worker["loaded_models"] = ["m"]
        self.worker["allocations"] = [{"model_key": "m", "healthy": False}]
        self.assertFalse(self.candidate()["hot"])

    def test_blocked_model_never_planned_ready(self):
        self.worker["loaded_models"] = ["m"]
        self.state["catalog"][0]["blocked"] = True
        row = fc.assessment(self.client, self.state)[0]
        self.assertEqual(row["readiness"], "blocked")
        self.client.request.assert_not_called()

    def test_measurements_are_worker_specific(self):
        self.state["metrics"] = [{"model_name": "m", "worker": "other", "hot_load_s": 1},
                                 {"model_name": "m", "worker": "gpu", "hot_load_s": 8}]
        self.assertEqual(self.candidate()["load_estimate_s"], 8)

    def test_partial_snapshot_is_visible(self):
        self.client.request.side_effect = fc.FleetError("HTTP 401")
        state = fc.snapshot(self.client)
        self.assertEqual(len(state["errors"]), 4)
        self.assertIsNone(state["workers"])

    def test_base_normalization(self):
        for base, expected in (("http://localhost:7002/v1", "http://localhost:7002"),
                               ("https://example/api/v1/", "https://example/api")):
            self.assertEqual(fc.Client(base).base, expected)

    def test_absolute_paths_rejected_before_network(self):
        client = fc.Client("https://example/api")
        for path in ("https://evil/", "//evil/", "/../secret", "/%2e%2e/secret"):
            with self.assertRaises(fc.FleetError):
                client.request(path)

    def test_auth_headers_and_no_redirect(self):
        response = Mock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(fc.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            fc.Client("http://localhost:7002", "api-secret", "operator-secret").request("/llm/workers")
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header("Authorization"), "Bearer api-secret")
            self.assertEqual(req.get_header("X-operator-token"), "operator-secret")
        self.assertIsNone(fc.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil"))

    def test_call_is_bounded_and_no_eviction_default(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            fc.dispatch(self.client, fc.parser().parse_args(["call", "m", "hello"]))
        body = self.client.request.call_args.args[2]
        self.assertTrue(body["no_makeroom"])
        self.assertEqual(body["max_tokens"], 128)

    def test_cli_dispatches_packaged_console(self):
        with patch.object(fc, "main", return_value=0) as main:
            self.assertEqual(cli.main(["console", "--base", "http://localhost:7002", "workers", "--json"]), 0)
        self.assertEqual(main.call_args.args[0], ["workers", "--json"])
        self.assertEqual(main.call_args.kwargs["cfg"].base, "http://localhost:7002")

    def test_exec_passes_argv_without_shell_and_scrubs_operator_token(self):
        self.client.base = "http://localhost:7002"
        self.client.key = "api-key"
        self.client.request.side_effect = None
        self.client.request.return_value = {"data": self.state["catalog"]}
        with patch.dict(fc.os.environ, {"HUGPY_OPERATOR_TOKEN": "secret"}), patch.object(fc.subprocess, "call", return_value=7) as run:
            result = fc.dispatch(self.client, fc.parser().parse_args(["exec", "m", "--", "program", "a; b"]))
        self.assertEqual(result, 7)
        self.assertEqual(run.call_args.args[0], ["program", "a; b"])
        self.assertNotIn("HUGPY_OPERATOR_TOKEN", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
