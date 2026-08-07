"""Fleet tools — the hugpy ML suite as model-invokable tools (Phase 1.5).

Call shapes lifted from the field-tested hugpyTab client
(abstract_ide/.../hugpyTab/src/generate.py: post_multipart, enqueue_image,
enqueue_scene, poll_job, cancel_job, fetch_media, output_uris, job_error,
_TASK_AMENITY) — the one proven implementation of the §2b wire contracts:

  * sync JSON amenities   POST /api/ml/<name>  {text|texts, model_key?}
  * sync file amenities   POST /api/ml/<name>  multipart (file + model_key)
  * async generation      POST /api/video/jobs/generate_image|generate_scene
                          -> {job_id}; poll GET /api/video/jobs/<id>;
                          bytes via GET /api/video/media?handle=<uri>

Doctrine:
  * Errors as data — every failure (HTTP body, `local_serving_disabled`,
    job error) is returned VERBATIM as a structured tool result. A capacity
    gap is the fleet's fault to report honestly, not the client's to mask.
  * remote_compute risk class — these calls spend fleet GPU. Async
    generation is capped per run (config.max_generations) with a structured
    refusal, and every enqueue journals its job_id IMMEDIATELY so a crash
    can never cause a duplicate job: resume re-polls, never re-enqueues.
  * model_key optional everywhere; omitted -> resolved from the /api/models
    catalog by task (§2b task sets), cached per registry build (≈ per run).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse

from . import (RISK_READONLY, RISK_REMOTE_COMPUTE, ToolContext,
               ToolInterrupted, ToolSpec)
from .fs import _confine

# ── job status sets (seed: generate.py) ─────────────────────────────────────
_DONE = {"done", "completed", "complete", "success", "succeeded", "finished"}
_FAIL = {"failed", "error", "cancelled", "canceled"}

POLL_INTERVAL = 2.0        # seconds between job polls
POLL_CEILING = 300         # polls; * 2s = 10 min hard ceiling per job

# ── task sets for model_key resolution (§2b + live catalog 2026-07-14) ─────
# Ordered: the first task with any catalog hit wins; within a task,
# primary_task matches are preferred over incidental tasks[] membership.
TOOL_TASKS: dict[str, tuple[str, ...]] = {
    "summarize":      ("text-summarization", "summarization",
                       "text2text-generation"),
    "keywords":       ("keyword-extraction", "text-summarization"),
    "embed":          ("feature-extraction", "sentence-similarity"),
    "similarity":     ("sentence-similarity", "feature-extraction"),
    "transcribe":     ("automatic-speech-recognition",),
    "classify":       ("image-classification",),
    "detect":         ("object-detection",),
    "segment":        ("image-segmentation",),
    "depth":          ("depth-estimation",),
    "vision":         ("image-text-to-text",),
    "generate_image": ("text-to-image",),
    # No dedicated video-task model is in the catalog today; the scene
    # pipeline renders frames with a text-to-image model and assembles them
    # (assemble:true), so t2i is a legitimate fallback resolver here.
    "generate_scene": ("image-to-video", "video-generation", "text-to-video",
                       "text-to-image"),
}


def _model_tasks(m: dict) -> set:
    ts = set(t for t in (m.get("tasks") or []) if t)
    if m.get("primary_task"):
        ts.add(m["primary_task"])
    return ts


def _err(msg) -> str:
    return json.dumps({"error": msg})


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode(errors="replace")[:500]
    except Exception:
        return ""


def _http_error_json(exc: urllib.error.HTTPError):
    """(parsed_dict_or_None, raw_500). The oracle endpoints answer 4xx with
    typed JSON bodies (400 malformed/ineligible, 404 unknown capability,
    422 CAPABILITY_GAP with a scorecard) — those are RESULTS for the model,
    not transport noise, so the whole body is read and parsed rather than
    truncated like _http_error_body's transport summary."""
    try:
        raw = exc.read().decode(errors="replace")
    except Exception:
        return None, ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, raw[:500]
    return (parsed if isinstance(parsed, dict) else None), raw[:500]


def output_uris(result: dict) -> list[str]:
    """Server refs for a job's outputs (+ assembled movie). Seed: generate.py."""
    uris = []
    for o in (result.get("outputs") or []):
        if isinstance(o, dict):
            u = o.get("uri") or o.get("url") or o.get("path")
            if u:
                uris.append(u)
        elif isinstance(o, str):
            uris.append(o)
    mv = result.get("movie")
    if isinstance(mv, dict):
        u = mv.get("uri") or mv.get("url") or mv.get("path")
        if u:
            uris.append(u)
    elif isinstance(mv, str):
        uris.append(mv)
    return uris


def job_error(result: dict):
    e = result.get("error")
    if isinstance(e, dict):
        return e.get("message") or e.get("code") or json.dumps(e)
    return e


def _sniff_ext(data: bytes, uri: str = "") -> str:
    """Extension from magic bytes first (the truth), URI suffix second."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp":
        return ".mp4"
    ext = os.path.splitext(urllib.parse.urlsplit(uri).path)[1]
    return ext if ext and len(ext) <= 5 else ".bin"


class FleetTools:
    """One instance per registry build. Holds the per-run catalog cache so
    model_key resolution costs at most one /api/models GET per run."""

    def __init__(self, gateway, workspace: str):
        self.gw = gateway
        self.workspace = os.path.realpath(workspace)
        self._catalog: list | None = None

    # ── catalog / model_key resolution ───────────────────────────────────
    def _load_catalog(self) -> list:
        if self._catalog is None:
            data = self.gw.api_json("/api/models", timeout=30)
            if isinstance(data, dict):
                data = data.get("models") or data.get("data") or []
            self._catalog = data if isinstance(data, list) else []
        return self._catalog

    def resolve_model_key(self, tool: str, model_key: str = "") -> tuple[str, str]:
        """(model_key, error). Explicit key wins untouched; otherwise the
        catalog is searched by the tool's task set. Unresolvable is an ERROR
        naming the tasks searched — guessing a model for a GPU call would be
        spending someone's compute on a hunch (fail-closed)."""
        if model_key:
            return model_key, ""
        tasks = TOOL_TASKS.get(tool, ())
        try:
            catalog = self._load_catalog()
        except Exception as exc:
            return "", ("could not load the model catalog to pick a default "
                        "model for %s (%s); pass model_key explicitly" % (tool, exc))
        for task in tasks:
            primary = [m for m in catalog if m.get("primary_task") == task]
            secondary = [m for m in catalog
                         if task in _model_tasks(m) and m not in primary]
            for m in primary + secondary:
                if m.get("model_key"):
                    return m["model_key"], ""
        return "", ("no model in the catalog handles any of the tasks %s "
                    "(needed by %s); pass model_key explicitly"
                    % (list(tasks), tool))

    # ── shared HTTP with errors-as-data ──────────────────────────────────
    def _ml_json(self, amenity: str, payload: dict, timeout: int | None = None):
        """POST /api/ml/<amenity>; returns (response_dict, error_str).
        `timeout` overrides the gateway default — the RAG embed path (P2.6)
        needs a tight bound so a dead endpoint degrades, never stalls."""
        try:
            res = self.gw.api_json("/api/ml/%s" % amenity, method="POST",
                                   payload=payload, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return None, "%s HTTP %s: %s" % (amenity, exc.code,
                                             _http_error_body(exc))
        except Exception as exc:
            return None, "%s request failed: %s" % (amenity, exc)
        if isinstance(res, dict) and res.get("ok") is False:
            # e.g. local_serving_disabled — the fleet's own words, verbatim.
            return None, str(res.get("error") or res.get("reason") or res)
        return res if isinstance(res, dict) else {"result": res}, ""

    def _ml_file(self, amenity: str, real_path: str, model_key: str):
        try:
            res = self.gw.api_multipart("/api/ml/%s" % amenity, real_path,
                                        {"model_key": model_key})
        except urllib.error.HTTPError as exc:
            return None, "%s HTTP %s: %s" % (amenity, exc.code,
                                             _http_error_body(exc))
        except Exception as exc:
            return None, "%s request failed: %s" % (amenity, exc)
        if isinstance(res, dict) and res.get("ok") is False:
            return None, str(res.get("error") or res.get("reason") or res)
        return res if isinstance(res, dict) else {"result": res}, ""

    def _save_artifact(self, name: str, data: bytes) -> str:
        """Write bytes under <workspace>/artifacts/, return the relative path
        (what the model cites). The artifacts dir keeps generated media out
        of the user's source tree."""
        art_dir = os.path.join(self.workspace, "artifacts")
        os.makedirs(art_dir, exist_ok=True)
        path = os.path.join(art_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return os.path.relpath(path, self.workspace)

    def _fetch_ref(self, uri: str, stem: str):
        """Fetch a server media ref, save as artifact. (rel_path, size, err).
        Always goes through /api/video/media on OUR origin — even for refs
        that look like absolute URLs. A job result must never redirect the
        agent's Bearer key to a foreign host."""
        try:
            data = self.gw.api_bytes(
                "/api/video/media?handle=" + urllib.parse.quote(uri, safe=""))
        except urllib.error.HTTPError as exc:
            return "", 0, "fetch of %s failed: HTTP %s %s" % (
                uri, exc.code, _http_error_body(exc))
        except Exception as exc:
            return "", 0, "fetch of %s failed: %s" % (uri, exc)
        rel = self._save_artifact(stem + _sniff_ext(data, uri), data)
        return rel, len(data), ""

    # ── sync JSON amenities ──────────────────────────────────────────────
    def summarize(self, text: str, model_key: str = "", full: bool = False) -> str:
        mk, err = self.resolve_model_key("summarize", model_key)
        if err:
            return _err(err)
        res, err = self._ml_json("summarize", {"text": text, "model_key": mk})
        if err:
            return _err(err)
        if full:
            return json.dumps(res)[:8000]
        out = res.get("summary") or res.get("text") or res.get("result")
        if isinstance(out, str) and out.strip():
            return json.dumps({"summary": out.strip(), "model_key": mk})
        return json.dumps(res)[:4000]

    def keywords(self, text: str, model_key: str = "", full: bool = False) -> str:
        mk, err = self.resolve_model_key("keywords", model_key)
        if err:
            return _err(err)
        res, err = self._ml_json("keywords", {"text": text, "model_key": mk})
        if err:
            return _err(err)
        if full:
            return json.dumps(res)[:8000]
        # The live /ml/keywords backend returns keyphrases under `combined`
        # (keybert/spacy); older/other shapes used `keywords`/`result`.
        kws = res.get("keywords") or res.get("result") or res.get("combined")
        if isinstance(kws, list):
            return json.dumps({"keywords": kws, "model_key": mk})
        return json.dumps(res)[:4000]

    def embed(self, text: str, model_key: str = "", full: bool = False) -> str:
        mk, err = self.resolve_model_key("embed", model_key)
        if err:
            return _err(err)
        res, err = self._ml_json("embed", {"text": text, "model_key": mk})
        if err:
            return _err(err)
        embs = res.get("embeddings") or []
        vec = embs[0] if embs and isinstance(embs[0], list) else embs
        if not isinstance(vec, list) or not vec:
            return json.dumps(res)[:4000]
        if full:
            return json.dumps({"vector": vec, "dims": len(vec),
                               "model_key": res.get("model_key") or mk})
        norm = sum(x * x for x in vec) ** 0.5
        return json.dumps({"dims": len(vec), "l2_norm": round(norm, 4),
                           "head": [round(x, 4) for x in vec[:8]],
                           "model_key": res.get("model_key") or mk,
                           "note": "pass full=true for the whole vector"})

    def similarity(self, text_a: str, text_b: str, model_key: str = "",
                   full: bool = False) -> str:
        mk, err = self.resolve_model_key("similarity", model_key)
        if err:
            return _err(err)
        res, err = self._ml_json("similarity",
                                 {"texts": [text_a, text_b], "model_key": mk})
        if err:
            return _err(err)
        if full:
            return json.dumps(res)[:8000]
        for key in ("similarity", "score", "cosine"):
            v = res.get(key)
            if isinstance(v, (int, float)):
                return json.dumps({"similarity": round(float(v), 6),
                                   "model_key": mk})
            if isinstance(v, list) and v and isinstance(v[0], (int, float)):
                return json.dumps({"similarity": round(float(v[0]), 6),
                                   "model_key": mk})
        return json.dumps(res)[:4000]

    # ── sync file amenities ──────────────────────────────────────────────
    def _file_amenity(self, amenity: str, path_arg: str, model_key: str,
                      full: bool = False) -> str:
        try:
            real = _confine(self.workspace, path_arg)
        except ValueError as exc:
            return _err(str(exc))
        if not os.path.isfile(real):
            return _err("no such file in the workspace: %s" % path_arg)
        mk, err = self.resolve_model_key(amenity, model_key)
        if err:
            return _err(err)
        res, err = self._ml_file(amenity, real, mk)
        if err:
            return _err(err)
        if full:
            return json.dumps(res)[:8000]
        text = res.get("text")
        if isinstance(text, str) and text.strip():
            return json.dumps({"text": text.strip(), "model_key": mk})
        # Depth/segment style: the result is an image ref — fetch it into
        # the workspace so the model (and the user) has a real file path.
        uris = output_uris(res)
        if uris:
            stem = "%s-%s" % (amenity,
                              os.path.splitext(os.path.basename(real))[0])
            rel, size, ferr = self._fetch_ref(uris[0], stem)
            if ferr:
                return json.dumps({"outputs": uris, "error": ferr})
            return json.dumps({"artifact": rel, "bytes": size,
                               "model_key": mk, "outputs": len(uris)})
        return json.dumps(res)[:4000]

    def transcribe(self, audio_path: str, model_key: str = "",
                   full: bool = False) -> str:
        return self._file_amenity("transcribe", audio_path, model_key, full)

    def classify(self, image_path: str, model_key: str = "",
                 full: bool = False) -> str:
        return self._file_amenity("classify", image_path, model_key, full)

    def detect(self, image_path: str, model_key: str = "",
               full: bool = False) -> str:
        return self._file_amenity("detect", image_path, model_key, full)

    def segment(self, image_path: str, model_key: str = "",
                full: bool = False) -> str:
        return self._file_amenity("segment", image_path, model_key, full)

    def depth(self, image_path: str, model_key: str = "",
              full: bool = False) -> str:
        return self._file_amenity("depth", image_path, model_key, full)

    # ── async generation (enqueue -> poll -> fetch) ──────────────────────
    def _generation_guard(self, tool: str, ctx: ToolContext):
        """Cap check. Counts journaled enqueue states (one per real job) so
        replays/resumes never double-count. Returns error string or ''."""
        states = ctx.run_states()
        used = sum(1 for s in states.values() if s.get("kind") == "generation")
        if used >= max(0, int(ctx.max_generations)):
            return ("generation cap reached: this run already enqueued %d "
                    "generation job(s) (limit %d, HUGPY_MAX_GENERATIONS). "
                    "Refusing %s — work with the artifacts you already have, "
                    "or finish and let the operator raise the cap."
                    % (used, ctx.max_generations, tool))
        return ""

    def _enqueue(self, endpoint: str, body: dict):
        """(job_id, error). POST /api/video/jobs/<endpoint> per §2b."""
        try:
            res = self.gw.api_json("/api/video/jobs/%s" % endpoint,
                                   method="POST", payload=body)
        except urllib.error.HTTPError as exc:
            return "", "%s HTTP %s: %s" % (endpoint, exc.code,
                                           _http_error_body(exc))
        except Exception as exc:
            return "", "%s enqueue failed: %s" % (endpoint, exc)
        if isinstance(res, dict) and res.get("ok") is False:
            return "", str(res.get("error") or res)
        job_id = (res or {}).get("job_id")
        if not job_id:
            return "", "no job_id returned by %s: %s" % (
                endpoint, json.dumps(res)[:300])
        return str(job_id), ""

    def _poll_to_completion(self, job_id: str, ctx: ToolContext):
        """(final_job_dict, error). ~2s cadence, 10min ceiling, SIGINT-safe:
        an operator stop raises ToolInterrupted WITHOUT cancelling the job —
        the job_id is journaled, so resume re-polls and the GPU work already
        spent is not thrown away."""
        for i in range(POLL_CEILING):
            if ctx.stop():
                raise ToolInterrupted("stop requested while polling job %s"
                                      % job_id)
            try:
                j = self.gw.api_json("/api/video/jobs/%s" % job_id, timeout=30)
            except Exception as exc:
                # transient poll failure: brief grace, then keep polling
                time.sleep(POLL_INTERVAL)
                if i >= POLL_CEILING - 2:
                    return None, "polling job %s failed: %s" % (job_id, exc)
                continue
            st = str(j.get("status") or j.get("state") or "?").lower()
            ctx.on_event("job_progress", job_id, st, j.get("progress"))
            if st in _DONE or st in _FAIL:
                return j, ""
            time.sleep(POLL_INTERVAL)
        return None, ("timed out after %ds waiting for job %s; it may still "
                      "complete server-side (GET /api/video/jobs/%s)"
                      % (int(POLL_CEILING * POLL_INTERVAL), job_id, job_id))

    def _run_generation(self, tool: str, endpoint: str, body: dict,
                        ctx: ToolContext, meta: dict) -> str:
        """Shared enqueue->journal->poll->fetch skeleton for both generators.

        CRITICAL ordering: the job_id is journaled via ctx.set_state THE
        MOMENT enqueue returns, before the first poll. A crash anywhere
        after enqueue leaves a pending call WITH a job_id, and resume lands
        back here, finds the state, and RE-POLLS — it must never re-enqueue
        (that is a duplicate GPU job, the exact incident class we just had).
        """
        state = ctx.get_state()
        if state and state.get("job_id"):
            job_id = state["job_id"]            # resume path: re-attach
        else:
            guard = self._generation_guard(tool, ctx)
            if guard:
                return _err(guard)
            job_id, err = self._enqueue(endpoint, body)
            if err:
                return _err(err)
            ctx.set_state({"kind": "generation", "job_id": job_id,
                           "tool": tool, "enqueued_at": time.time()})
        job, err = self._poll_to_completion(job_id, ctx)
        if err:
            return _err(err)
        result = job.get("result") if isinstance(job.get("result"), dict) else job
        status = str(job.get("status") or "").lower()
        jerr = job_error(result)
        if jerr or status in _FAIL or result.get("ok") is False:
            # local_serving_disabled etc. — the fleet's words, verbatim.
            return json.dumps({"error": jerr or "job %s ended %s" % (job_id, status),
                               "job_id": job_id})
        uris = output_uris(result)
        if not uris:
            return json.dumps({"error": "job %s finished with no outputs" % job_id,
                               "job_id": job_id,
                               "result": json.dumps(result)[:500]})
        # scene: prefer the assembled movie (last uri appended by output_uris)
        uri = uris[-1] if (tool == "generate_scene" and result.get("movie")) \
            else uris[0]
        rel, size, ferr = self._fetch_ref(uri, job_id)
        if ferr:
            return json.dumps({"error": ferr, "job_id": job_id, "outputs": uris})
        out = {"artifact": rel, "bytes": size, "job_id": job_id,
               "outputs": len(uris)}
        out.update(meta)
        return json.dumps(out)

    def generate_image(self, prompt: str, width: int = 512, height: int = 512,
                       steps: int = 20, guidance: float = 4.0,
                       negative: str = "", seed: int = -1,
                       model_key: str = "", _context: ToolContext = None) -> str:
        ctx = _context or ToolContext()
        mk, err = self.resolve_model_key("generate_image", model_key)
        if err:
            return _err(err)
        body = {"parts": [{"kind": "text", "text": prompt}], "model_id": mk,
                "width": int(width), "height": int(height),
                "steps": int(steps), "guidance": float(guidance)}
        if negative:
            body["negative"] = negative
        if seed is not None and int(seed) >= 0:
            body["seed"] = int(seed)
        return self._run_generation(
            "generate_image", "generate_image", body, ctx,
            {"width": int(width), "height": int(height), "model_key": mk})

    def generate_scene(self, prompt: str, n_frames: int = 8, fps: int = 8,
                       width: int = 512, height: int = 512, steps: int = 20,
                       guidance: float = 4.0, negative: str = "",
                       model_key: str = "", _context: ToolContext = None) -> str:
        ctx = _context or ToolContext()
        mk, err = self.resolve_model_key("generate_scene", model_key)
        if err:
            return _err(err)
        body = {"parts": [{"kind": "text", "text": prompt}], "model_id": mk,
                "width": int(width), "height": int(height),
                "steps": int(steps), "guidance": float(guidance),
                "n_frames": int(n_frames), "fps": int(fps), "assemble": True}
        if negative:
            body["negative"] = negative
        return self._run_generation(
            "generate_scene", "generate_scene", body, ctx,
            {"n_frames": int(n_frames), "fps": int(fps), "model_key": mk})

    # ── vision + models_list (Phase 1, kept) ─────────────────────────────
    def vision(self, image_path: str, question: str, model_key: str = "") -> str:
        try:
            real = _confine(self.workspace, image_path)
        except ValueError as exc:
            return _err(str(exc))
        # Both "prompt" and "question" field names are sent: the amenity's
        # accepted field name isn't in the route survey, servers ignore
        # unknown form fields, and one upload beats a probe round-trip.
        fields = {"prompt": question, "question": question}
        if model_key:
            fields["model_key"] = model_key
        try:
            res = self.gw.api_multipart("/api/ml/vision", real, fields)
        except urllib.error.HTTPError as exc:
            return _err("vision HTTP %s: %s" % (exc.code, _http_error_body(exc)))
        except Exception as exc:
            return _err("vision request failed: %s" % exc)
        if isinstance(res, dict):
            if res.get("ok") is False:
                return _err(res.get("error") or res)
            text = res.get("text") or res.get("answer") or res.get("result")
            if isinstance(text, str) and text.strip():
                return text
        return json.dumps(res)[:4000]

    def models_list(self) -> str:
        entries = self.gw.models(refresh=True)
        out = []
        for e in entries:
            out.append({k: e.get(k) for k in
                        ("id", "name", "context_length", "tasks", "task")
                        if e.get(k) is not None})
        return json.dumps({"models": out, "count": len(out)})

    # ── oracle: route-to-best (k93, server side k90/k91) ─────────────────
    def _oracle_shape(self, res: dict) -> str:
        """Label the response shape and surface the scorecard verdict at the
        TOP level (hard_pass / diagnosis / repair_code) — the brain must see
        quality without digging into the card. The server payload rides
        along verbatim below the surfaced keys (the loop consumes the JSON).
        The three keys are always present; None means 'no scorecard came
        back' (typed 400s carry none)."""
        card = res.get("scorecard") if isinstance(res.get("scorecard"), dict) \
            else {}
        route = res.get("route") if isinstance(res.get("route"), dict) else {}
        if res.get("execution") == "deferred":
            status = "deferred"          # routed, NOT run (video.* today)
        elif route.get("execution") == "gap":
            status = "capability_gap"    # no eligible route; see scorecard
        elif res.get("ok") is False:
            status = "error"             # typed refusal, verbatim below
        else:
            status = "executed"
        out = {"oracle_status": status,
               "hard_pass": card.get("hard_pass"),
               "diagnosis": card.get("diagnosis"),
               "repair_code": card.get("repair_code")}
        out.update(res)
        return json.dumps(out)[:16000]

    def oracle_route(self, prompt: str, inputs: list = None,
                     capability: str = "", model_id: str = "",
                     quality: str = "", evaluate: bool = None,
                     repair: bool = None) -> str:
        if inputs is not None and not isinstance(inputs, list):
            return _err("inputs must be a list of {kind, uri|text} objects")
        body = {"prompt": prompt}
        if inputs:
            body["inputs"] = inputs
        if capability:
            body["capability"] = capability
        if model_id:
            body["model_id"] = model_id
        if quality:
            body["quality"] = quality
        # k92 passthroughs: forwarded only when supplied — a server without
        # the evaluator kernel yet ignores unknown JSON fields.
        if evaluate is not None:
            body["evaluate"] = bool(evaluate)
        if repair is not None:
            body["repair"] = bool(repair)
        try:
            res = self.gw.api_json("/api/oracle/route", method="POST",
                                   payload=body)
        except urllib.error.HTTPError as exc:
            parsed, raw = _http_error_json(exc)
            if parsed is not None:
                # typed 400 / 422 gap: a result shape, not a transport fault
                return self._oracle_shape(parsed)
            return _err("oracle_route HTTP %s: %s" % (exc.code, raw))
        except Exception as exc:
            return _err("oracle_route request failed: %s" % exc)
        if not isinstance(res, dict):
            return json.dumps({"result": res})[:8000]
        return self._oracle_shape(res)

    def oracle_capabilities(self, capability: str = "") -> str:
        path = "/api/oracle/capabilities"
        if capability:
            path += "?capability=" + urllib.parse.quote(capability, safe="")
        try:
            res = self.gw.api_json(path, timeout=30)
        except urllib.error.HTTPError as exc:
            parsed, raw = _http_error_json(exc)
            if parsed is not None:
                # typed 404 names the unknown capability + the known list
                return json.dumps(parsed)[:8000]
            return _err("oracle_capabilities HTTP %s: %s" % (exc.code, raw))
        except Exception as exc:
            return _err("oracle_capabilities request failed: %s" % exc)
        return json.dumps(res)[:16000]


# ── registry wiring ─────────────────────────────────────────────────────────
def _p(**props) -> dict:
    required = props.pop("_required", [])
    return {"type": "object", "properties": props, "required": required}

_MK = {"type": "string",
       "description": "optional model; default picked from the catalog"}
_FULL = {"type": "boolean", "description": "true = full raw response"}


def specs(gateway, workspace: str) -> list[ToolSpec]:
    ft = FleetTools(gateway, workspace)
    rc = RISK_REMOTE_COMPUTE
    return [
        # ── text amenities ────────────────────────────────────────────
        ToolSpec("summarize",
                 "Summarize a long text with a fleet model. "
                 'Example: summarize(text="<the text>").',
                 _p(text={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["text"]),
                 ft.summarize, rc),
        ToolSpec("keywords",
                 "Extract keywords from a text. Returns a keyword list.",
                 _p(text={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["text"]),
                 ft.keywords, rc),
        ToolSpec("embed",
                 "Embedding vector for a text (dims, norm, first values; "
                 "full=true for the whole vector).",
                 _p(text={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["text"]),
                 ft.embed, rc),
        ToolSpec("similarity",
                 "Semantic similarity score (0..1) between two texts. "
                 'Example: similarity(text_a="cat", text_b="kitten").',
                 _p(text_a={"type": "string"}, text_b={"type": "string"},
                    model_key=_MK, full=_FULL, _required=["text_a", "text_b"]),
                 ft.similarity, rc),
        # ── file amenities ────────────────────────────────────────────
        ToolSpec("transcribe",
                 "Speech-to-text for an audio/video file in the workspace. "
                 'Example: transcribe(audio_path="clips/call.mp3").',
                 _p(audio_path={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["audio_path"]),
                 ft.transcribe, rc),
        ToolSpec("classify",
                 "Classify what an image in the workspace shows (labels).",
                 _p(image_path={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["image_path"]),
                 ft.classify, rc),
        ToolSpec("detect",
                 "Detect objects in a workspace image (boxes + labels).",
                 _p(image_path={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["image_path"]),
                 ft.detect, rc),
        ToolSpec("segment",
                 "Segment a workspace image; the mask image is saved to "
                 "artifacts/ and its path returned.",
                 _p(image_path={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["image_path"]),
                 ft.segment, rc),
        ToolSpec("depth",
                 "Depth map for a workspace image; saved to artifacts/, "
                 "path returned.",
                 _p(image_path={"type": "string"}, model_key=_MK, full=_FULL,
                    _required=["image_path"]),
                 ft.depth, rc),
        # ── async generation ──────────────────────────────────────────
        ToolSpec("generate_image",
                 "Generate an image from a text prompt (slow: ~1-5 min GPU "
                 "job, capped per run). Saves to artifacts/<job>.png and "
                 'returns the path. Example: generate_image(prompt="a red '
                 'barn at dusk", width=512, height=512).',
                 _p(prompt={"type": "string"},
                    width={"type": "integer"}, height={"type": "integer"},
                    steps={"type": "integer"}, guidance={"type": "number"},
                    negative={"type": "string"}, seed={"type": "integer"},
                    model_key=_MK, _required=["prompt"]),
                 ft.generate_image, rc, needs_context=True),
        ToolSpec("generate_scene",
                 "Generate a short video clip from a prompt (very slow GPU "
                 "job, capped per run). Saves the mp4 to artifacts/ and "
                 "returns the path.",
                 _p(prompt={"type": "string"},
                    n_frames={"type": "integer"}, fps={"type": "integer"},
                    width={"type": "integer"}, height={"type": "integer"},
                    steps={"type": "integer"}, guidance={"type": "number"},
                    negative={"type": "string"}, model_key=_MK,
                    _required=["prompt"]),
                 ft.generate_scene, rc, needs_context=True),
        # ── Phase-1 tools, kept ───────────────────────────────────────
        ToolSpec("vision",
                 "Ask a question about an image file in the workspace; "
                 "returns the vision model's text answer.",
                 _p(image_path={"type": "string"}, question={"type": "string"},
                    model_key=_MK, _required=["image_path", "question"]),
                 ft.vision, rc),
        ToolSpec("models_list",
                 "List the models currently available on the fleet.",
                 _p(), ft.models_list, RISK_READONLY),
        # ── oracle: route-to-best (k93) ───────────────────────────────
        ToolSpec("oracle_route",
                 "One call that routes a request to the BEST fleet model and "
                 "runs it: the oracle infers the capability from the prompt "
                 "(or takes an explicit `capability`), picks the best "
                 "eligible model, executes, and returns artifacts + receipt "
                 "+ a quality scorecard. The result always surfaces "
                 "hard_pass/diagnosis/repair_code at the top level. PREFER "
                 "this over the single-purpose tools (vision, summarize, "
                 "transcribe, classify, ...) when the ask is multimodal, "
                 "when you do not know which model is best, or when you "
                 "need quality evidence; keep the narrow tools for tight "
                 "single-task calls where you already know exactly what "
                 "you want. `inputs` URIs are SERVER paths on shared "
                 "storage (NOT agent-local files — upload/produce them "
                 "first, or use the narrow file tools). oracle_status is "
                 "one of executed | deferred (video.*: routed, not run) | "
                 "capability_gap | error. Example: oracle_route(prompt="
                 '"summarize this image", inputs=[{"kind": "image", '
                 '"uri": "/srv/shared/photo.png"}]).',
                 _p(prompt={"type": "string",
                            "description": "what you want done, plain words"},
                    inputs={"type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {"type": "string",
                                             "description":
                                             "text|image|audio|video|url"},
                                    "uri": {"type": "string",
                                            "description":
                                            "SERVER path/URI on shared "
                                            "storage"},
                                    "text": {"type": "string",
                                             "description":
                                             "inline text (kind=text)"}},
                                "required": ["kind"]},
                            "description": "optional input refs; uri values "
                                           "are SERVER paths, not agent-"
                                           "local files"},
                    capability={"type": "string",
                                "description": "optional explicit capability "
                                               "(e.g. audio.transcribe); "
                                               "wins over inference"},
                    model_id={"type": "string",
                              "description": "optional: force a model; "
                                             "ineligible ids come back as a "
                                             "typed error with the eligible "
                                             "list"},
                    quality={"type": "string",
                             "description": "preview | balanced | best "
                                            "(default balanced)"},
                    evaluate={"type": "boolean",
                              "description": "optional: run the judge "
                                             "evaluation pass"},
                    repair={"type": "boolean",
                            "description": "optional: allow one bounded "
                                           "repair loop on a failed card"},
                    _required=["prompt"]),
                 ft.oracle_route, rc),
        ToolSpec("oracle_capabilities",
                 "List the oracle's capability catalog: what the fleet can "
                 "do right now (name, accepted/produced kinds, model ids, "
                 "eligibility with reasons). Check it before oracle_route "
                 "when unsure a capability exists or why one is ineligible.",
                 _p(capability={"type": "string",
                                "description": "optional: filter to this one "
                                               "capability name"}),
                 ft.oracle_capabilities, RISK_READONLY),
    ]
