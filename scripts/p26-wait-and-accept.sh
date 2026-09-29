#!/usr/bin/env bash
# P2.6 embed-RAG memory — LIVE ACCEPTANCE (restaged, durable, in-repo).
#
# Restages the acceptance script lost to a reboot (the old copy lived in a
# wiped /tmp). Design, per STATUS.md milestone P2.6:
#
#   1. WAIT-GATE: poll the fleet until the agent brain model is servable as an
#      LLM (chat completion returns real text). The embed model (all-minilm,
#      op worker) is a SECOND hard dependency for the vector-index criterion,
#      so we wait for it too, within the same deadline. Records the worker each
#      actually served from.
#   2. ACCEPTANCE BODY: three distinct `remember` operations (distinct facts)
#      driven as real agent runs -> assert the SQLite vector index row count
#      rose by exactly 3 -> a `recall` agent run that retrieves >=1 remembered
#      fact. Captures row counts before/after, recall output, audit JSONL.
#
# Secrets: the API key is loaded from the gitignored repo .env into the
# environment (never echoed, never written to any evidence file). Config .env
# is workspace-relative, so a fresh acceptance workspace needs the key in env.
#
# Usage:  bash scripts/p26-wait-and-accept.sh [workspace_dir]
#   - default workspace: ${TMPDIR:-/tmp}/p26-accept-<timestamp> (fresh, so the
#     vector count starts clean and the repo's own memory/ is never touched).
#   - HUGPY_P26_DEADLINE_MIN overrides the wait timeout (default 45 min).
#
# Does NOT git-commit. Leaves the working tree for keeper review. Test memory
# rows are left in the throwaway workspace (the harness has no delete tool);
# the workspace is disposable, so nothing durable is polluted.
set -uo pipefail

REPO="/srv/share/projects/blackbird/hugpy_agent"
cd "$REPO" || { echo "FATAL: repo not found: $REPO" >&2; exit 3; }
export PYTHONPATH="$REPO/src"

# Load secrets (HUGPY_API_KEY, HUGPY_DISCORD_SESSION) WITHOUT echoing them.
if [ -f "$REPO/.env" ]; then set -a; . "$REPO/.env"; set +a; fi

WS="${1:-${TMPDIR:-/tmp}/p26-accept-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$WS" || { echo "FATAL: cannot create workspace $WS" >&2; exit 3; }

export HUGPY_WORKSPACE="$WS"
export HUGPY_POLICY=auto            # remember is RISK_WRITE; auto lets it run unattended
export HUGPY_AUDIT_VERBOSE=1        # truncated plaintext in audit lines (facts are not secret)
export HUGPY_P26_DEADLINE_MIN="${HUGPY_P26_DEADLINE_MIN:-45}"

echo "=== P2.6 live acceptance ==="
echo "repo=$REPO"
echo "workspace=$WS"
echo "base(from config)= (resolved by harness; see below)"
echo "deadline=${HUGPY_P26_DEADLINE_MIN} min"
echo "started=$(date -u +%FT%TZ)"
echo

python3 - <<'PYEOF'
import json, os, sys, time, sqlite3, urllib.request

sys.path.insert(0, os.path.join(os.environ["PYTHONPATH"].split(":")[0]))
from hugpy_agent.config import load_config
from hugpy_agent.gateway import Gateway
from hugpy_agent.loop import AgentLoop
from hugpy_agent.rag import fleet_embedder, default_vectors_path

WS = os.environ["HUGPY_WORKSPACE"]
CENTRAL_LAN = "http://192.168.1.250:7002"      # LAN hub for cheap serving-state polls
BRAIN_KEY = "flux2-klein-9b-uncensored-text-encoder"
EMBED_KEY = "all-minilm-l6-v2"
DEADLINE = time.time() + float(os.environ["HUGPY_P26_DEADLINE_MIN"]) * 60
POLL_S = 40

cfg = load_config()   # workspace = WS via env; key from env
gw = Gateway.from_config(cfg)
print("base=%s  model=%s  key=%s  rag_enabled=%s  policy=%s"
      % (cfg.base, cfg.model, "set" if cfg.api_key else "MISSING",
         cfg.rag_enabled, cfg.policy_mode), flush=True)
try:
    print("resolved routes: %s" % (gw.resolve(),), flush=True)
except Exception as e:
    print("route resolve error: %r" % e, flush=True)


def central(path, timeout=8):
    try:
        req = urllib.request.Request(CENTRAL_LAN + path,
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode(errors="replace") or "{}")
    except Exception as e:
        return {"_err": str(e)}


def serving_mode(key):
    d = central("/api/llm/serving/%s" % key)
    return d.get("mode"), d.get("endpoint")


def worker_for(model_substr):
    """Which worker holds a live slot for a model_key containing model_substr."""
    d = central("/api/llm/workers")
    if not isinstance(d, list):
        return None, None
    for w in d:
        for a in (w.get("allocations") or []):
            mk = str(a.get("model_key") or "")
            if model_substr.lower() in mk.lower() and a.get("kind") == "slot":
                return w.get("name") or w.get("id"), mk
    # fall back: any allocation (ram) mentioning it
    for w in d:
        for a in (w.get("allocations") or []):
            mk = str(a.get("model_key") or "")
            if model_substr.lower() in mk.lower():
                return (w.get("name") or w.get("id")) + "(ram)", mk
    return None, None


_ERR_MARKERS = ("[error", "worker could not", "could not complete",
                "local model serving is disabled", "no registered worker",
                "overloaded", "try again")


def brain_confirm():
    """The fleet returns HTTP 200 with an ERROR STRING in the content when the
    routed worker can't serve (model still loading/unavailable) — so ok+text
    is a false positive. Require the asked-for token and reject error markers."""
    t = time.time()
    res = gw.chat([{"role": "user", "content": "Reply with exactly the single "
                    "word: READY"}], max_tokens=16, timeout=180)
    dt = time.time() - t
    txt = (res.text or "").strip()
    low = txt.lower()
    is_err = any(m in low for m in _ERR_MARKERS)
    ready = bool(res.ok and ("ready" in low) and not is_err)
    detail = (res.error or "") if not txt else txt
    return ready, dt, txt[:80], detail[:180]


def embed_confirm():
    t = time.time()
    emb = fleet_embedder(gw, WS)
    try:
        v = emb("readiness probe")
        return True, time.time() - t, len(v), ""
    except Exception as e:
        return False, time.time() - t, 0, str(e)[:200]


# ── WAIT-GATE ────────────────────────────────────────────────────────────
print("\n--- WAIT-GATE (brain=%s, embed=%s) ---" % (BRAIN_KEY, EMBED_KEY), flush=True)
brain_ok = embed_ok = False
brain_worker = embed_worker = None
brain_latency = embed_dims = None
wait_start = time.time()
cycle = 0
while time.time() < DEADLINE:
    cycle += 1
    bmode, bep = serving_mode(BRAIN_KEY)
    emode, eep = serving_mode(EMBED_KEY)
    left = int(DEADLINE - time.time())
    line = "[cycle %d | %ds left] brain.mode=%s embed.mode=%s" % (cycle, left, bmode, emode)

    if not brain_ok:
        # attempt confirm whenever it might be servable (off => fast-fail)
        ok, dt, txt, err = brain_confirm()
        if ok:
            brain_ok = True
            brain_latency = dt
            brain_worker, bmk = worker_for("flux2-klein")
            line += " | BRAIN READY (%.1fs, %r) worker=%s" % (dt, txt, brain_worker)
        else:
            line += " | brain not ready (%.1fs: %s)" % (dt, err or "empty")
    else:
        line += " | brain READY worker=%s" % brain_worker

    if not embed_ok:
        ok, dt, dims, err = embed_confirm()
        if ok:
            embed_ok = True
            embed_dims = dims
            embed_worker, emk = worker_for("all-minilm")
            line += " | EMBED READY (dims=%d) worker=%s" % (dims, embed_worker)
        else:
            line += " | embed not ready (%s)" % err
    else:
        line += " | embed READY worker=%s" % embed_worker

    print(line, flush=True)
    if brain_ok and embed_ok:
        break
    time.sleep(POLL_S)

wait_elapsed = time.time() - wait_start
print("\nwait-gate elapsed: %.0fs (%.1f min) | brain_ok=%s embed_ok=%s"
      % (wait_elapsed, wait_elapsed / 60, brain_ok, embed_ok), flush=True)

if not brain_ok:
    print("\nRESULT: TIMEOUT — the brain model never became servable as an LLM "
          "within the deadline. Acceptance NOT run.", flush=True)
    print("SUMMARY_JSON=" + json.dumps({
        "result": "TIMEOUT_BRAIN", "brain_ok": brain_ok, "embed_ok": embed_ok,
        "wait_seconds": round(wait_elapsed), "workspace": WS}), flush=True)
    sys.exit(2)

# ── ACCEPTANCE BODY ──────────────────────────────────────────────────────
vpath = default_vectors_path(WS)


def vcount():
    if not os.path.exists(vpath):
        return 0
    try:
        c = sqlite3.connect(vpath)
        n = c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        c.close()
        return int(n)
    except Exception as e:
        return "ERR:%s" % e


FACTS = [
    ("backup window",
     "Blackbird's nightly backup window starts at 03:30 UTC, right after the "
     "02:00 solar-vision evaluation run."),
    ("agent brain",
     "The hugpy-agent brain is the flux2-klein model, prompted with a "
     "/no_think suffix so it reliably emits tool calls."),
    ("embed worker",
     "The fleet text-embedding model all-minilm-l6-v2 runs on the op worker, "
     "which has no GPU."),
]
RECALL_QUERY = "What time does the nightly backup start?"
RECALL_EXPECT_TOKEN = "03:30"     # distinctive token of FACT 1

count_before = vcount()
print("\n--- ACCEPTANCE BODY ---")
print("vector rows BEFORE: %s" % count_before, flush=True)

events = []           # collect rag_error events across runs


def on_event(kind, *a):
    if kind == "rag_error":
        events.append(("rag_error",) + a)
        print("   [rag_error] %s" % (a,), flush=True)


run_reports = []
for i, (title, fact) in enumerate(FACTS, 1):
    task = ('Save exactly one fact to memory, then finish. Call the `remember` '
            'tool once with fact="%s" and title="%s". After the tool returns, '
            'call `final_answer` with the word done. Call no other tool.'
            % (fact, title))
    t = time.time()
    loop = AgentLoop(cfg, on_event=on_event)
    rep = loop.run(task)
    dt = time.time() - t
    n = vcount()
    print("remember #%d (%s): outcome=%s steps=%s tool_calls=%s %.1fs -> rows=%s"
          % (i, title, rep.get("outcome"), rep.get("steps"),
             rep.get("tool_calls"), dt, n), flush=True)
    run_reports.append({"i": i, "title": title, "outcome": rep.get("outcome"),
                        "steps": rep.get("steps"), "tool_calls": rep.get("tool_calls"),
                        "seconds": round(dt, 1), "rows_after": n,
                        "run_id": rep.get("run_id")})

count_after = vcount()
delta = (count_after - count_before) if isinstance(count_after, int) and isinstance(count_before, int) else "N/A"
print("\nvector rows AFTER 3 remembers: %s (delta=%s)" % (count_after, delta), flush=True)

# ── recall as an agent run ───────────────────────────────────────────────
recall_task = ('Search workspace memory. Call the `recall` tool once with '
               'query="%s". After it returns, call `final_answer` quoting the '
               'text of the single best-matching remembered fact from the tool '
               'result.' % RECALL_QUERY)
t = time.time()
loop = AgentLoop(cfg, on_event=on_event)
recall_rep = loop.run(recall_task)
recall_dt = time.time() - t
recall_answer = (recall_rep.get("answer") or "")
print("\nrecall run: outcome=%s steps=%s %.1fs" % (recall_rep.get("outcome"),
      recall_rep.get("steps"), recall_dt), flush=True)
print("recall final answer: %s" % recall_answer[:400], flush=True)

# ── independent (deterministic) recall via the harness RAG ───────────────
from hugpy_agent.rag import RagIndex
ri = RagIndex(vpath, fleet_embedder(gw, WS))
matches, rerr = ri.recall(RECALL_QUERY, k=3)
print("\nindependent recall (harness RagIndex) for %r:" % RECALL_QUERY, flush=True)
if matches is None:
    print("   RAG unavailable: %s" % rerr, flush=True)
else:
    for m in matches:
        print("   score=%.4f  %s" % (m["score"], m["text"][:100]), flush=True)

# ── audit evidence ───────────────────────────────────────────────────────
audit_path = os.path.join(WS, ".hugpy_agent", "audit.jsonl")
audit_lines = []
if os.path.exists(audit_path):
    with open(audit_path) as fh:
        for ln in fh:
            try:
                audit_lines.append(json.loads(ln))
            except Exception:
                pass
remember_audits = [a for a in audit_lines if a.get("tool") == "remember"]
recall_audits = [a for a in audit_lines if a.get("tool") == "recall"]
print("\naudit: %d total lines | remember=%d recall=%d"
      % (len(audit_lines), len(remember_audits), len(recall_audits)), flush=True)
print("remember audit lines (tool/decision/error/args_sha8/args_text):", flush=True)
distinct_sha = set()
for a in remember_audits:
    distinct_sha.add(a.get("args_sha256"))
    print("   decision=%s error=%s sha=%s args=%s"
          % (a.get("decision"), a.get("error_bool"),
             (a.get("args_sha256") or "")[:8],
             (a.get("args_text") or "")[:120]), flush=True)
for a in recall_audits:
    print("recall audit: decision=%s error=%s result=%s"
          % (a.get("decision"), a.get("error_bool"),
             (a.get("result_text") or "")[:200]), flush=True)

# markdown fact files (source of truth) evidence
memdir = os.path.join(WS, "memory")
md_files = sorted(f for f in os.listdir(memdir)) if os.path.isdir(memdir) else []
print("\nmemory/ markdown files: %s" % md_files, flush=True)

# ── criteria verdicts ────────────────────────────────────────────────────
c1 = (len(remember_audits) == 3 and len(distinct_sha) == 3
      and all(not a.get("error_bool") for a in remember_audits))
c2 = (delta == 3)
recall_hit_agent = RECALL_EXPECT_TOKEN in recall_answer
recall_hit_direct = bool(matches) and any(RECALL_EXPECT_TOKEN in m["text"] for m in (matches or []))
c3 = recall_hit_agent or recall_hit_direct

def PF(b): return "PASS" if b else "FAIL"
print("\n=== VERDICTS ===")
print("C1 three distinct remember tool-calls issued (no error): %s "
      "(audit remembers=%d distinct_args=%d)" % (PF(c1), len(remember_audits), len(distinct_sha)))
print("C2 vector index row count rose by exactly 3: %s (before=%s after=%s delta=%s)"
      % (PF(c2), count_before, count_after, delta))
print("C3 recall retrieved >=1 remembered fact (token %r): %s "
      "(agent_answer_hit=%s direct_recall_hit=%s)"
      % (RECALL_EXPECT_TOKEN, PF(c3), recall_hit_agent, recall_hit_direct))
overall = "PASS" if (c1 and c2 and c3) else "FAIL"
print("OVERALL: %s" % overall)

print("SUMMARY_JSON=" + json.dumps({
    "result": overall,
    "brain_ok": brain_ok, "embed_ok": embed_ok,
    "brain_worker": brain_worker, "embed_worker": embed_worker,
    "brain_latency_s": round(brain_latency, 2) if brain_latency else None,
    "embed_dims": embed_dims,
    "wait_seconds": round(wait_elapsed),
    "count_before": count_before, "count_after": count_after, "delta": delta,
    "c1": c1, "c2": c2, "c3": c3,
    "remember_runs": run_reports,
    "recall_outcome": recall_rep.get("outcome"),
    "recall_answer_head": recall_answer[:200],
    "rag_errors": [list(e) for e in events],
    "workspace": WS,
}))
sys.exit(0 if overall == "PASS" else 1)
PYEOF
rc=$?
echo
echo "finished=$(date -u +%FT%TZ) rc=$rc"
exit $rc
