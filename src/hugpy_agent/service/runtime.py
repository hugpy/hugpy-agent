"""Durable sessions backed by the existing Hugpy Agent loop and policy gate."""
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from .profiles import CLIENTS, load_profiles, public_profile
from .providers import gateway


class Store:
    def __init__(self, root):
        self.path = root / "sessions.sqlite3"
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, profile TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    run_id TEXT, native_id TEXT, report TEXT);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_session ON events(session,id);
                UPDATE sessions SET status='interrupted' WHERE status IN ('running','waiting','stopping');
            ''')
        db.close()
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def query(self, sql, args=()):
        db = self.connect()
        try:
            with db:
                return [dict(row) for row in db.execute(sql, args)]
        finally:
            db.close()

    def session(self, sid):
        rows = self.query("SELECT * FROM sessions WHERE id=?", (sid,))
        if not rows:
            raise KeyError("Unknown session")
        row = rows[0]
        row["report"] = json.loads(row["report"]) if row["report"] else None
        return row

    def event(self, sid, kind, data):
        self.query("INSERT INTO events(session,ts,kind,data) VALUES(?,?,?,?)",
                   (sid, time.time(), kind, json.dumps(data, default=str)))

    def events(self, sid, after=0):
        rows = self.query("SELECT * FROM events WHERE session=? AND id>? ORDER BY id LIMIT 500", (sid, after))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows


class OperatorComms:
    def __init__(self, runtime, sid, control):
        self.runtime, self.sid, self.control = runtime, sid, control

    def configured(self):
        return True

    def ask(self, question, options, timeout=None, stop=None):
        pending = {"id": uuid.uuid4().hex, "question": question, "options": options}
        signal = threading.Event()
        with self.runtime.lock:
            self.control["pending"] = pending
            self.control["answer_signal"] = signal
            self.control.pop("answer", None)
            self.runtime.store.query("UPDATE sessions SET status='waiting' WHERE id=?", (self.sid,))
        self.runtime.store.event(self.sid, "question", pending)
        deadline = time.monotonic() + (timeout or self.runtime.cfg.ask_timeout)
        try:
            while time.monotonic() < deadline:
                if self.control["stop"].is_set() or (stop and stop()):
                    return {"answered": False, "choice": None, "timed_out": False, "error": "Stopped"}
                if signal.wait(0.25):
                    return {"answered": True, "choice": self.control["answer"], "timed_out": False}
            return {"answered": False, "choice": None, "timed_out": True}
        finally:
            with self.runtime.lock:
                self.control.pop("pending", None)
                self.runtime.store.query("UPDATE sessions SET status='running' WHERE id=?", (self.sid,))


class Runtime:
    def __init__(self, cfg, root, profiles_path):
        self.cfg = cfg
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.root)
        self.profiles_path = profiles_path
        self.lock = threading.RLock()
        self.active = {}
        self.closing = False
        self.catalog = {}
        self.catalog_at = 0
        self.catalog_lock = threading.Lock()
        self.profiles()  # reject invalid configuration at startup

    def profiles(self):
        default, profiles = load_profiles(self.profiles_path)
        with self.catalog_lock:
            if time.monotonic() - self.catalog_at > 60:
                self.catalog_at = time.monotonic()
                self.catalog_thread = threading.Thread(target=self._refresh_catalog, args=(profiles,), daemon=True)
                self.catalog_thread.start()
            catalog = {k: v for k, v in self.catalog.items() if k.split(":", 1)[0] in profiles}
        return default, {**profiles, **catalog}

    def _refresh_catalog(self, profiles):
        from ..gateway import Gateway
        catalog = dict(self.catalog)
        for name, profile in profiles.items():
            if not profile.get("discover_models") or profile["protocol"] not in ("hugpy", "openai-chat"):
                continue
            try:
                client = (Gateway(profile["base_url"], os.environ.get(profile.get("api_key_env", ""), ""), timeout=5)
                          if profile["protocol"] == "hugpy" else gateway(profile, self.cfg))
                discovered = {}
                for model in client.models(refresh=True):
                    mid = model.get("id")
                    if not mid or model.get("blocked") or model.get("archived") or model.get("serveable") is False:
                        continue
                    tasks = model.get("tasks") or [model.get("task") or "text-generation"]
                    if "text-generation" not in tasks:
                        continue
                    key = name + ":" + mid
                    discovered[key] = dict(profile, model=mid, label=profile["label"] + " · " + mid,
                        context_length=max(profile["max_tokens"] + 1, int(model.get("context_length") or profile["context_length"])))
                catalog = {k: v for k, v in catalog.items() if not k.startswith(name + ":")}
                catalog.update(discovered)
            except (OSError, ValueError, TypeError):
                pass  # Keep the last catalog during an outage; UI never waits for discovery.
        with self.catalog_lock:
            self.catalog = catalog

    def state(self):
        default, profiles = self.profiles()
        return {"service": "hugpy-agent", "protocol_version": 1, "root": str(self.root),
                "workspace": self.cfg.workspace,
                "config": {"default_profile": default, "default_model": profiles[default].get("model", "")},
                "profiles": [public_profile(n, p) for n, p in profiles.items()],
                "sessions": self.store.query("SELECT id,profile,status,created,updated FROM sessions ORDER BY updated DESC LIMIT 100")}

    def create(self, profile=None):
        default, profiles = self.profiles()
        name = profile or default
        if name not in profiles:
            raise ValueError("Unknown profile")
        public = public_profile(name, profiles[name])
        if not public["available"]:
            raise ValueError(public["reason"])
        sid, now = uuid.uuid4().hex, time.time()
        self.store.query("INSERT INTO sessions(id,profile,status,created,updated) VALUES(?,?,'idle',?,?)", (sid, name, now, now))
        return self.view(sid)

    def view(self, sid, after=0):
        with self.lock:
            session = self.store.session(sid)
            session["pending"] = self.active.get(sid, {}).get("pending")
        session["events"] = self.store.events(sid, after)
        return session

    def select_profile(self, sid, name):
        with self.lock:
            session = self.store.session(sid)
            if sid in self.active:
                raise RuntimeError("Wait for the turn to finish before changing model")
            _, profiles = self.profiles()
            if name not in profiles or not public_profile(name, profiles[name])["available"]:
                raise ValueError("Profile is not available")
            if name != session["profile"]:
                self.store.query("UPDATE sessions SET profile=?,native_id=NULL,run_id=NULL,status='idle',updated=? WHERE id=?",
                                 (name, time.time(), sid))
                self.store.event(sid, "model", name)
        return self.view(sid)

    def start(self, sid, text="", resume=False):
        if not isinstance(text, str) or (not resume and not text.strip()) or len(text) > 100000:
            raise ValueError("A nonempty message of at most 100000 characters is required")
        with self.lock:
            session = self.store.session(sid)
            if self.closing or sid in self.active or len(self.active) >= 4:
                raise RuntimeError("Service is busy; wait for the active turn or stop it")
            _, profiles = self.profiles()
            profile = profiles.get(session["profile"])
            if not profile or not public_profile(session["profile"], profile)["available"]:
                raise ValueError("Session profile is no longer available")
            if resume and (profile["protocol"] in CLIENTS or not session["run_id"] or session["status"] != "interrupted"):
                raise ValueError("Only interrupted Hugpy Agent runs can be resumed")
            control = {"stop": threading.Event()}
            self.active[sid] = control
            if not resume:
                self.store.event(sid, "user", text)
            self.store.query("UPDATE sessions SET status='running',updated=? WHERE id=?", (time.time(), sid))
            thread = threading.Thread(target=self._run, args=(session, profile, text, resume, control), daemon=True)
            control["thread"] = thread
            thread.start()
        return self.view(sid)

    def answer(self, sid, question_id, choice):
        with self.lock:
            control = self.active.get(sid, {})
            pending = control.get("pending")
            if not pending or pending["id"] != question_id:
                raise ValueError("Question is no longer pending")
            if not isinstance(choice, str) or not choice.strip() or len(choice) > 10000:
                raise ValueError("Answer must be a nonempty string")
            # Policy questions accept only their offered choices; free text for ask_operator.
            if "Approve" in pending["options"] and choice not in pending["options"]:
                raise ValueError("Choose one of the offered policy answers")
            if control["answer_signal"].is_set():
                raise ValueError("Question has already been answered")
            control["answer"] = choice
            control["answer_signal"].set()
        self.store.event(sid, "answer", choice)
        return {"ok": True}

    def stop(self, sid):
        self.store.session(sid)
        with self.lock:
            control = self.active.get(sid)
            if control:
                control["stop"].set()
                if control.get("loop"):
                    control["loop"].stop_requested = True
                if control.get("process"):
                    from .native import stop_process
                    stop_process(control["process"])
                self.store.query("UPDATE sessions SET status='stopping' WHERE id=?", (sid,))
        return {"ok": True, "note": "Stopping after the current operation"}

    def shutdown(self):
        with self.lock:
            self.closing = True
            ids = list(self.active)
        for sid in ids:
            self.stop(sid)

    def _run(self, session, profile, text, resume, control):
        sid = session["id"]
        loop = None
        def emit(kind, *data):
            if kind == "run_start":
                self.store.query("UPDATE sessions SET run_id=? WHERE id=?", (data[0], sid))
            self.store.event(sid, kind, data[0] if len(data) == 1 else list(data))
        try:
            if profile["protocol"] in CLIENTS:
                from .native import run
                if not session["native_id"]:
                    prior = self.store.query("SELECT kind,data FROM events WHERE session=? AND kind IN ('user','reply') ORDER BY id DESC LIMIT 17", (sid,))
                    history = json.dumps([{r["kind"]: json.loads(r["data"])} for r in reversed(prior[1:])])
                    if len(prior) > 1:
                        text = "Prior conversation:\n" + history[-16000:] + "\nCurrent request:\n" + text
                report = run(profile, self.cfg.workspace, text, session["native_id"], emit, control)
            else:
                from ..loop import AgentLoop
                from ..journal import Journal
                from ..adapter import Adapter
                from ..tools import Registry, ToolSpec, RISK_READONLY, fs, http, shell, toolserver
                cfg = replace(self.cfg, model=profile["model"], base=profile["base_url"],
                              api_key=os.environ.get(profile.get("api_key_env", ""), ""),
                              max_tokens=profile["max_tokens"], ctx_fallback=profile["context_length"],
                              model_2="", brains=[], tools_mode="prompted", rag_enabled=False)
                comms = OperatorComms(self, sid, control)
                registry = Registry()
                for spec in [*fs.specs(cfg.workspace), shell.spec(cfg.workspace), http.spec(),
                             *toolserver.specs(cfg, on_event=emit)]:
                    registry.register(spec)
                registry.register(ToolSpec("final_answer", "Finish the task with your complete answer.",
                    {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
                    lambda answer: answer, RISK_READONLY))
                registry.register(ToolSpec("ask_operator", "Ask the operator a question.",
                    {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question", "options"]},
                    lambda question, options: json.dumps(comms.ask(question, options, stop=control["stop"].is_set)), RISK_READONLY))
                run_dir = self.root / "runs" / sid
                run_dir.mkdir(parents=True, exist_ok=True)
                loop = AgentLoop(cfg, gateway=gateway(profile, cfg), registry=registry,
                                 journal=Journal(str(run_dir / "journal.db")), adapter=Adapter("prompted"),
                                 on_event=emit, comms=comms, conversation=True)
                control["loop"] = loop
                loop.stop_requested = control["stop"].is_set()
                if resume:
                    report = loop.resume(session["run_id"])
                else:
                    prior = self.store.query("SELECT kind,data FROM events WHERE session=? AND kind IN ('user','reply') ORDER BY id DESC LIMIT 17", (sid,))
                    prior = list(reversed(prior[1:]))  # newest user text is the task below
                    # Bound conversational history separately from the full durable transcript.
                    history = json.dumps([{r["kind"]: json.loads(r["data"])} for r in prior])
                    budget = max(1024, min(2000000, (profile["context_length"] - profile["max_tokens"]) * 2))
                    task = ("Prior conversation (possibly shortened):\n" + history[-budget:] + "\n\nCurrent request:\n" + text) if prior else text
                    report = loop.run(task)
            if report.get("answer"):
                self.store.event(sid, "reply", report["answer"])
        except Exception as exc:
            report = {"outcome": "aborted", "error": str(exc)}
        finally:
            if loop:
                loop.journal.close()
        with self.lock:
            status = "interrupted" if control["stop"].is_set() else report.get("outcome", "aborted")
            self.store.query("UPDATE sessions SET status=?,updated=?,native_id=COALESCE(?,native_id),report=? WHERE id=?",
                             (status, time.time(), report.get("native_id"), json.dumps(report), sid))
            self.store.event(sid, "done", report)
            self.active.pop(sid, None)
