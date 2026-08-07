"""Fleet ML tools, offline: amenity payload shapes, file-arg jail, artifact
saving, async generation lifecycle, per-run cap, resume re-poll vs re-enqueue,
model_key resolution. All HTTP is stubbed — no network."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import io
import json
import os
import tempfile
import unittest
import urllib.error

from hugpy_agent.journal import Journal
from hugpy_agent.tools import Registry, ToolContext, ToolInterrupted
from hugpy_agent.tools import fleet as fleet_mod
from hugpy_agent.tools.fleet import FleetTools, specs

PNG = b"\x89PNG\r\n\x1a\n" + b"fakepixels"
CATALOG = [
    {"model_key": "flan-t5-large", "primary_task": "text-summarization",
     "tasks": ["text-summarization", "text2text-generation"]},
    {"model_key": "all-minilm-l6-v2", "primary_task": "feature-extraction",
     "tasks": ["feature-extraction", "sentence-similarity",
               "keyword-extraction"]},
    {"model_key": "whisper-large-v3-turbo",
     "primary_task": "automatic-speech-recognition",
     "tasks": ["automatic-speech-recognition"]},
    {"model_key": "depth-anything-v2-small",
     "primary_task": "depth-estimation", "tasks": ["depth-estimation"]},
    {"model_key": "sd-turbo", "primary_task": "text-to-image",
     "tasks": ["text-to-image", "image-to-image"]},
]


class StubGateway:
    """Records every fleet-facing request; routes to test-supplied handlers."""

    def __init__(self):
        self.base = "fake://"
        self.json_calls = []        # (path, method, payload)
        self.multipart_calls = []   # (path, filepath, fields)
        self.bytes_calls = []       # path
        self.on_json = lambda path, method, payload: {}
        self.on_multipart = lambda path, filepath, fields: {}
        self.on_bytes = lambda path: b""

    def api_json(self, path, method="GET", payload=None, timeout=None):
        self.json_calls.append((path, method, payload))
        if path == "/api/models":
            return CATALOG
        return self.on_json(path, method, payload)

    def api_multipart(self, path, filepath, fields=None, timeout=None):
        self.multipart_calls.append((path, filepath, fields))
        return self.on_multipart(path, filepath, fields)

    def api_bytes(self, path, timeout=None, max_bytes=None):
        self.bytes_calls.append(path)
        return self.on_bytes(path)


class FleetHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.gw = StubGateway()
        self.ft = FleetTools(self.gw, self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, journal=None, run_id="r1", idem="k1", max_gen=2, stop=None):
        return ToolContext(journal=journal, run_id=run_id, idem_key=idem,
                           max_generations=max_gen,
                           stop=stop or (lambda: False))


class SyncJsonAmenityTests(FleetHarness):
    def test_summarize_payload_and_extraction(self):
        self.gw.on_json = lambda p, m, b: {"ok": True, "summary": "short."}
        out = json.loads(self.ft.summarize("long text here"))
        self.assertEqual(out["summary"], "short.")
        path, method, payload = self.gw.json_calls[-1]
        self.assertEqual((path, method), ("/api/ml/summarize", "POST"))
        self.assertEqual(payload["text"], "long text here")
        self.assertEqual(payload["model_key"], "flan-t5-large")  # resolved

    def test_keywords_list(self):
        self.gw.on_json = lambda p, m, b: {"ok": True, "keywords": ["a", "b"]}
        out = json.loads(self.ft.keywords("txt"))
        self.assertEqual(out["keywords"], ["a", "b"])
        self.assertEqual(self.gw.json_calls[-1][2]["model_key"],
                         "all-minilm-l6-v2")   # keyword-extraction task

    def test_keywords_combined_field(self):
        # The live keybert backend returns keyphrases under `combined`.
        self.gw.on_json = lambda p, m, b: {"ok": True,
                                           "combined": ["sea stars", "tide pools"],
                                           "backends_used": ["keybert"]}
        out = json.loads(self.ft.keywords("txt"))
        self.assertEqual(out["keywords"], ["sea stars", "tide pools"])

    def test_embed_summary_and_full(self):
        vec = [0.6, 0.8] + [0.0] * 10
        self.gw.on_json = lambda p, m, b: {"embeddings": [vec],
                                           "model_key": "all-minilm-l6-v2"}
        out = json.loads(self.ft.embed("hello"))
        self.assertEqual(out["dims"], 12)
        self.assertAlmostEqual(out["l2_norm"], 1.0, places=4)
        self.assertEqual(out["head"][:2], [0.6, 0.8])
        full = json.loads(self.ft.embed("hello", full=True))
        self.assertEqual(len(full["vector"]), 12)

    def test_similarity_texts_pair_payload(self):
        self.gw.on_json = lambda p, m, b: {"ok": True, "similarity": 0.87}
        out = json.loads(self.ft.similarity("cat", "kitten"))
        self.assertAlmostEqual(out["similarity"], 0.87)
        payload = self.gw.json_calls[-1][2]
        self.assertEqual(payload["texts"], ["cat", "kitten"])

    def test_explicit_model_key_wins_no_catalog_call(self):
        self.gw.on_json = lambda p, m, b: {"ok": True, "summary": "s"}
        self.ft.summarize("t", model_key="my-model")
        paths = [c[0] for c in self.gw.json_calls]
        self.assertNotIn("/api/models", paths)
        self.assertEqual(self.gw.json_calls[-1][2]["model_key"], "my-model")

    def test_capacity_error_verbatim(self):
        self.gw.on_json = lambda p, m, b: {"ok": False,
                                           "error": "local_serving_disabled"}
        out = json.loads(self.ft.summarize("t"))
        self.assertEqual(out["error"], "local_serving_disabled")

    def test_unresolvable_task_names_it(self):
        # capture BEFORE patching: self.gw.__class__ IS StubGateway, so
        # reading the attribute back in finally would restore the patch itself
        orig = StubGateway.api_json
        self.gw.__class__.api_json = _no_catalog_api_json
        try:
            out = json.loads(self.ft.summarize("t"))
        finally:
            self.gw.__class__.api_json = orig
        self.assertIn("text-summarization", out["error"])

    def test_catalog_cached_per_instance(self):
        self.gw.on_json = lambda p, m, b: {"ok": True, "summary": "s"}
        self.ft.summarize("a")
        self.ft.keywords("b")
        self.assertEqual([c[0] for c in self.gw.json_calls].count("/api/models"), 1)


def _no_catalog_api_json(self, path, method="GET", payload=None, timeout=None):
    self.json_calls.append((path, method, payload))
    if path == "/api/models":
        return []          # empty catalog: nothing resolvable
    return self.on_json(path, method, payload)


class FileAmenityTests(FleetHarness):
    def _mkfile(self, rel, data=b"x"):
        path = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(path) or self.ws, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_transcribe_multipart_shape(self):
        self._mkfile("a.mp3")
        self.gw.on_multipart = lambda p, f, fl: {"ok": True, "text": "hi there"}
        out = json.loads(self.ft.transcribe("a.mp3"))
        self.assertEqual(out["text"], "hi there")
        path, filepath, fields = self.gw.multipart_calls[-1]
        self.assertEqual(path, "/api/ml/transcribe")
        self.assertTrue(filepath.endswith("a.mp3"))
        self.assertEqual(fields["model_key"], "whisper-large-v3-turbo")

    def test_jail_rejects_escaping_path(self):
        out = json.loads(self.ft.transcribe("../../etc/passwd"))
        self.assertIn("escapes the workspace", out["error"])
        self.assertEqual(self.gw.multipart_calls, [])   # never uploaded

    def test_missing_file_is_data(self):
        out = json.loads(self.ft.depth("nope.png"))
        self.assertIn("no such file", out["error"])

    def test_depth_image_ref_fetched_to_artifacts(self):
        self._mkfile("photo.png")
        self.gw.on_multipart = lambda p, f, fl: {
            "ok": True, "outputs": [{"uri": "/srv/out/depth123.png"}]}
        self.gw.on_bytes = lambda p: PNG
        out = json.loads(self.ft.depth("photo.png"))
        self.assertEqual(out["artifact"], "artifacts/depth-photo.png")
        self.assertEqual(out["bytes"], len(PNG))
        with open(os.path.join(self.ws, out["artifact"]), "rb") as fh:
            self.assertEqual(fh.read(), PNG)
        self.assertIn("handle=%2Fsrv%2Fout%2Fdepth123.png",
                      self.gw.bytes_calls[0])


class GenerationTests(FleetHarness):
    def _script_job(self, statuses, result=None, job_id="job42"):
        """Route enqueue + successive polls."""
        state = {"polls": 0}
        result = result if result is not None else {
            "outputs": [{"uri": "/out/img.png"}]}

        def on_json(path, method, payload):
            if path.endswith("/jobs/generate_image") or \
               path.endswith("/jobs/generate_scene"):
                return {"job_id": job_id}
            if "/api/video/jobs/" in path:
                st = statuses[min(state["polls"], len(statuses) - 1)]
                state["polls"] += 1
                return {"job_id": job_id, "status": st, "result": result}
            raise AssertionError("unexpected path " + path)
        self.gw.on_json = on_json
        self.gw.on_bytes = lambda p: PNG
        return state

    def setUp(self):
        super().setUp()
        fleet_mod.POLL_INTERVAL = 0.0    # no real sleeping in tests
        self.db = os.path.join(self.ws, "j.db")
        self.journal = Journal(self.db)
        self.run_id = self.journal.create_run("t", "m")

    def tearDown(self):
        fleet_mod.POLL_INTERVAL = 2.0
        self.journal.close()
        super().tearDown()

    def test_enqueue_payload_shape(self):
        self._script_job(["done"])
        ctx = self.ctx(self.journal, self.run_id, "k-shape")
        self.ft.generate_image("a red barn", width=512, height=384, steps=4,
                               guidance=1.0, seed=7, _context=ctx)
        enq = [c for c in self.gw.json_calls
               if c[0].endswith("generate_image")][0]
        body = enq[2]
        self.assertEqual(body["parts"], [{"kind": "text", "text": "a red barn"}])
        self.assertEqual(body["model_id"], "sd-turbo")   # resolved by task
        self.assertEqual((body["width"], body["height"]), (512, 384))
        self.assertEqual(body["seed"], 7)

    def test_success_saves_artifact(self):
        self._script_job(["queued", "running", "done"])
        ctx = self.ctx(self.journal, self.run_id, "k-ok")
        out = json.loads(self.ft.generate_image("x", _context=ctx))
        self.assertEqual(out["artifact"], "artifacts/job42.png")  # magic sniff
        self.assertEqual(out["job_id"], "job42")
        self.assertTrue(os.path.exists(os.path.join(self.ws, out["artifact"])))

    def test_job_id_journaled_before_first_poll(self):
        """The crash-window guarantee: state exists as soon as enqueue
        returns. Simulated by failing the FIRST poll hard."""
        def on_json(path, method, payload):
            if path.endswith("/jobs/generate_image"):
                return {"job_id": "j9"}
            raise RuntimeError("boom on poll")
        self.gw.on_json = on_json
        ctx = self.ctx(self.journal, self.run_id, "k-crash")
        out = json.loads(self.ft.generate_image("x", _context=ctx))
        self.assertIn("error", out)                       # poll failed
        state = self.journal.get_call_state(self.run_id, "k-crash")
        self.assertEqual(state["job_id"], "j9")           # ...but state is there
        self.assertEqual(state["kind"], "generation")

    def test_resume_repolls_never_reenqueues(self):
        """A pending call WITH a journaled job_id must re-poll that job."""
        self.journal.set_call_state(self.run_id, "k-res",
                                    {"kind": "generation", "job_id": "old7"})
        polled = []

        def on_json(path, method, payload):
            if path.endswith(("generate_image", "generate_scene")):
                raise AssertionError("re-enqueued a duplicate job!")
            polled.append(path)
            return {"job_id": "old7", "status": "done",
                    "result": {"outputs": [{"uri": "/o/x.png"}]}}
        self.gw.on_json = on_json
        self.gw.on_bytes = lambda p: PNG
        ctx = self.ctx(self.journal, self.run_id, "k-res")
        out = json.loads(self.ft.generate_image("x", _context=ctx))
        self.assertEqual(out["job_id"], "old7")
        self.assertTrue(any("/api/video/jobs/old7" in p for p in polled))

    def test_generation_cap_refusal_is_structured(self):
        self._script_job(["done"])
        for i in range(2):
            ctx = self.ctx(self.journal, self.run_id, "k-cap%d" % i, max_gen=2)
            out = json.loads(self.ft.generate_image("x", _context=ctx))
            self.assertIn("artifact", out)
        ctx = self.ctx(self.journal, self.run_id, "k-cap2", max_gen=2)
        out = json.loads(self.ft.generate_image("x", _context=ctx))
        self.assertIn("generation cap reached", out["error"])
        self.assertIn("HUGPY_MAX_GENERATIONS", out["error"])
        # refusal enqueued nothing:
        enq = [c for c in self.gw.json_calls if c[0].endswith("generate_image")]
        self.assertEqual(len(enq), 2)

    def test_resume_exempt_from_cap(self):
        """Re-attaching to an existing job is not new spend; the cap must
        not block a resume even at the limit."""
        self.journal.set_call_state(self.run_id, "g1",
                                    {"kind": "generation", "job_id": "a"})
        self.journal.set_call_state(self.run_id, "g2",
                                    {"kind": "generation", "job_id": "b"})
        self._script_job(["done"], job_id="b")
        ctx = self.ctx(self.journal, self.run_id, "g2", max_gen=2)
        out = json.loads(self.ft.generate_image("x", _context=ctx))
        self.assertEqual(out["job_id"], "b")

    def test_job_failure_verbatim(self):
        self._script_job(["failed"],
                         result={"error": "local_serving_disabled"})
        ctx = self.ctx(self.journal, self.run_id, "k-fail")
        out = json.loads(self.ft.generate_image("x", _context=ctx))
        self.assertEqual(out["error"], "local_serving_disabled")
        self.assertEqual(out["job_id"], "job42")

    def test_stop_raises_toolinterrupted_without_cancel(self):
        self._script_job(["running"] * 50)
        cancels = []
        orig = self.gw.on_json

        def on_json(path, method, payload):
            if path.endswith("/cancel"):
                cancels.append(path)
            return orig(path, method, payload)
        self.gw.on_json = on_json
        ctx = self.ctx(self.journal, self.run_id, "k-stop",
                       stop=lambda: True)
        with self.assertRaises(ToolInterrupted):
            self.ft.generate_image("x", _context=ctx)
        self.assertEqual(cancels, [])    # job left running for resume

    def test_scene_prefers_movie(self):
        self._script_job(["done"], result={
            "outputs": [{"uri": "/o/frame0.png"}],
            "movie": {"uri": "/o/clip.mp4"}})
        self.gw.on_bytes = lambda p: b"\x00\x00\x00\x18ftypmp42" + b"x" * 8
        ctx = self.ctx(self.journal, self.run_id, "k-scene")
        out = json.loads(self.ft.generate_scene("x", _context=ctx))
        self.assertEqual(out["artifact"], "artifacts/job42.mp4")
        self.assertIn("handle=%2Fo%2Fclip.mp4", self.gw.bytes_calls[-1])
        enq = [c for c in self.gw.json_calls
               if c[0].endswith("generate_scene")][0]
        self.assertTrue(enq[2]["assemble"])
        self.assertEqual(enq[2]["n_frames"], 8)


def _http_err(code, body_dict=None, body_raw=b"<html>boom</html>"):
    """A real urllib HTTPError with a readable body, like urlopen raises."""
    data = json.dumps(body_dict).encode() if body_dict is not None else body_raw
    return urllib.error.HTTPError("fake://oracle", code, "err", None,
                                  io.BytesIO(data))


class OracleTests(FleetHarness):
    """oracle_route / oracle_capabilities (k93): request shapes, the four
    labelled response shapes, top-level scorecard surfacing, error
    normalization. All HTTP stubbed, same as the rest of the file."""

    EXECUTED = {
        "ok": True,
        "goal": {"objective": "summarize this image"},
        "route": {"capability": "image.caption", "model_id": "blip2",
                  "execution": "sync", "rationale": "best eligible"},
        "artifacts": [{"kind": "text", "uri": "", "sha256": "abc",
                       "text": "a red barn at dusk"}],
        "receipt": {"capability": "image.caption", "model_id": "blip2"},
        "scorecard": {"hard_pass": True, "checks": [], "judge_results": [],
                      "diagnosis": None, "repair_code": None},
    }

    def test_route_happy_path_surfaces_scorecard(self):
        self.gw.on_json = lambda p, m, b: dict(self.EXECUTED)
        out = json.loads(self.ft.oracle_route(
            "summarize this image",
            inputs=[{"kind": "image", "uri": "/srv/shared/photo.png"}],
            quality="best"))
        # the surfaced trio sits at the TOP level, no digging
        self.assertEqual(out["oracle_status"], "executed")
        self.assertIs(out["hard_pass"], True)
        self.assertIsNone(out["diagnosis"])
        self.assertIsNone(out["repair_code"])
        # the server payload rides along verbatim
        self.assertEqual(out["artifacts"][0]["text"], "a red barn at dusk")
        self.assertEqual(out["receipt"]["model_id"], "blip2")
        self.assertEqual(out["scorecard"]["hard_pass"], True)
        path, method, payload = self.gw.json_calls[-1]
        self.assertEqual((path, method), ("/api/oracle/route", "POST"))
        self.assertEqual(payload["prompt"], "summarize this image")
        self.assertEqual(payload["inputs"][0]["uri"], "/srv/shared/photo.png")
        self.assertEqual(payload["quality"], "best")

    def test_route_k92_flags_forwarded_only_when_given(self):
        self.gw.on_json = lambda p, m, b: dict(self.EXECUTED)
        self.ft.oracle_route("x")
        self.assertNotIn("evaluate", self.gw.json_calls[-1][2])
        self.assertNotIn("repair", self.gw.json_calls[-1][2])
        self.ft.oracle_route("x", evaluate=True, repair=False)
        payload = self.gw.json_calls[-1][2]
        self.assertIs(payload["evaluate"], True)
        self.assertIs(payload["repair"], False)

    def test_route_typed_400_is_data_not_exception(self):
        body = {"ok": False,
                "error": "model_id 'nope' is not eligible for image.caption",
                "eligible": ["blip2", "llava"]}

        def on_json(p, m, b):
            raise _http_err(400, body)
        self.gw.on_json = on_json
        out = json.loads(self.ft.oracle_route("caption", model_id="nope"))
        self.assertEqual(out["oracle_status"], "error")
        self.assertIn("not eligible", out["error"])       # verbatim
        self.assertEqual(out["eligible"], ["blip2", "llava"])
        self.assertIsNone(out["hard_pass"])               # no card on a 400

    def test_route_deferred_shape_labelled(self):
        deferred = {
            "ok": True, "routed": "video.generate", "execution": "deferred",
            "reason": "video capabilities execute through the studio job "
                      "pipeline",
            "binding": {"model_id": "wan2.1", "model_ids": ["wan2.1"]},
            "route": {"capability": "video.generate", "execution": "deferred"},
            "scorecard": {"hard_pass": False, "judge_results": [],
                          "diagnosis": "video execution is deferred by k91 "
                                       "scope",
                          "repair_code": None},
        }
        self.gw.on_json = lambda p, m, b: deferred
        out = json.loads(self.ft.oracle_route("make a video of a barn"))
        self.assertEqual(out["oracle_status"], "deferred")
        self.assertIs(out["hard_pass"], False)
        self.assertIn("deferred", out["diagnosis"])
        self.assertEqual(out["binding"]["model_id"], "wan2.1")

    def test_route_gap_shape_labelled_with_repair_code(self):
        gap = {"ok": False, "error": "capability gap",
               "goal": {"objective": "diarize this"},
               "route": {"capability": "audio.diarize", "execution": "gap",
                         "reasons": ["no model handles audio.diarize"]},
               "scorecard": {"hard_pass": False,
                             "diagnosis": "no eligible route for "
                                          "'audio.diarize'",
                             "repair_code": "CAPABILITY_GAP"}}

        def on_json(p, m, b):
            raise _http_err(422, gap)
        self.gw.on_json = on_json
        out = json.loads(self.ft.oracle_route("diarize this"))
        self.assertEqual(out["oracle_status"], "capability_gap")
        self.assertIs(out["hard_pass"], False)
        self.assertEqual(out["repair_code"], "CAPABILITY_GAP")
        self.assertEqual(out["route"]["reasons"],
                         ["no model handles audio.diarize"])

    def test_route_non_json_http_error_normalized(self):
        def on_json(p, m, b):
            raise _http_err(502, body_raw=b"<html>bad gateway</html>")
        self.gw.on_json = on_json
        out = json.loads(self.ft.oracle_route("x"))
        self.assertIn("oracle_route HTTP 502", out["error"])
        self.assertIn("bad gateway", out["error"])

    def test_route_network_error_normalized(self):
        def on_json(p, m, b):
            raise OSError("connection refused")
        self.gw.on_json = on_json
        out = json.loads(self.ft.oracle_route("x"))
        self.assertIn("oracle_route request failed", out["error"])
        self.assertIn("connection refused", out["error"])

    def test_route_rejects_non_list_inputs(self):
        out = json.loads(self.ft.oracle_route("x", inputs="photo.png"))
        self.assertIn("inputs must be a list", out["error"])
        self.assertEqual(self.gw.json_calls, [])          # never sent

    def test_capabilities_list_and_filter(self):
        caps = {"ok": True, "count": 1, "capabilities": [
            {"name": "image.caption", "accepts": ["image"],
             "produces": ["text"], "model_ids": ["blip2"],
             "eligibility": {"eligible": True, "reasons": []}}]}
        self.gw.on_json = lambda p, m, b: caps
        out = json.loads(self.ft.oracle_capabilities())
        self.assertEqual(out["capabilities"][0]["name"], "image.caption")
        self.assertEqual(self.gw.json_calls[-1][0], "/api/oracle/capabilities")
        self.ft.oracle_capabilities("image.caption")
        self.assertEqual(self.gw.json_calls[-1][0],
                         "/api/oracle/capabilities?capability=image.caption")

    def test_capabilities_typed_404_is_data(self):
        body = {"ok": False, "error": "unknown capability 'audio.diarize'",
                "known": ["image.caption", "text.summarize"]}

        def on_json(p, m, b):
            raise _http_err(404, body)
        self.gw.on_json = on_json
        out = json.loads(self.ft.oracle_capabilities("audio.diarize"))
        self.assertIn("unknown capability", out["error"])
        self.assertEqual(out["known"], ["image.caption", "text.summarize"])

    def test_capabilities_network_error_normalized(self):
        def on_json(p, m, b):
            raise OSError("connection refused")
        self.gw.on_json = on_json
        out = json.loads(self.ft.oracle_capabilities())
        self.assertIn("oracle_capabilities request failed", out["error"])


class RegistryWiringTests(FleetHarness):
    def test_all_tools_registered_with_remote_compute(self):
        reg = Registry()
        for s in specs(self.gw, self.ws):
            reg.register(s)
        for name in ("summarize", "keywords", "embed", "similarity",
                     "transcribe", "classify", "detect", "segment", "depth",
                     "generate_image", "generate_scene", "vision",
                     "oracle_route"):
            spec = reg.get(name)
            self.assertIsNotNone(spec, name)
            self.assertEqual(spec.risk_class, "remote_compute", name)
        self.assertTrue(reg.get("generate_image").needs_context)
        self.assertEqual(reg.get("models_list").risk_class, "readonly")
        self.assertEqual(reg.get("oracle_capabilities").risk_class, "readonly")

    def test_build_registry_exposes_oracle_tools(self):
        from hugpy_agent.tools import build_registry
        reg = build_registry(self.ws, self.gw)
        self.assertIn("oracle_route", reg.names())
        self.assertIn("oracle_capabilities", reg.names())
        self.assertEqual(reg.get("oracle_route").parameters["required"],
                         ["prompt"])

    def test_model_cannot_smuggle_context(self):
        """A '_context' key in model-supplied arguments must be stripped,
        not forwarded (it would collide with the harness's own kwarg)."""
        reg = Registry()
        for s in specs(self.gw, self.ws):
            reg.register(s)
        self.gw.on_json = lambda p, m, b: {"ok": True, "summary": "s"}
        out = reg.execute(reg.get("summarize"),
                          {"text": "t", "_context": "evil"})
        self.assertIn("summary", out)


if __name__ == "__main__":
    unittest.main()
