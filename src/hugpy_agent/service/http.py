"""Small authenticated HTTP surface; no provider SDK or client installed at startup."""
import argparse
import hmac
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..config import load_config
from .runtime import Runtime


def handler(runtime, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HugpyAgentServe/1"

        def log_message(self, *args):
            pass  # requests may contain private session identifiers

        def reply(self, status, doc, content_type="application/json"):
            body = json.dumps(doc).encode() if content_type == "application/json" else doc
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if token:
                return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)
            # Without a token only loopback Host headers and same-origin browser
            # requests are accepted. This also prevents DNS rebinding to localhost.
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            origin = self.headers.get("Origin")
            return host in ("localhost", "127.0.0.1", "::1") and (not origin or urlsplit(origin).netloc == self.headers.get("Host"))

        def body(self):
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= 131072:
                raise ValueError("Invalid request body length")
            doc = json.loads(self.rfile.read(n))
            if not isinstance(doc, dict):
                raise ValueError("Request body must be a JSON object")
            return doc

        def do_GET(self):
            self.dispatch(False)

        def do_POST(self):
            self.dispatch(True)

        def dispatch(self, post):
            try:
                parsed = urlsplit(self.path)
                path = parsed.path.rstrip("/")
                if not post and path in ("", "/index.html"):
                    return self.reply(200, Path(__file__).with_name("index.html").read_bytes(), "text/html; charset=utf-8")
                if not self.authorized():
                    return self.reply(401, {"error": "Service authentication required"})
                if not post and path == "/api/console/models":
                    profiles = runtime.state()["profiles"]
                    return self.reply(200, {"models": [
                        {"backend": "hugpy", "model": p["id"], "label": "Hugpy · " + p["label"]}
                        for p in profiles if p.get("available") and p["protocol"] == "hugpy"]})
                if not post and path in ("/api/state", "/api/profiles"):
                    return self.reply(200, runtime.state())
                if not post and path == "/api/tools":
                    return self.reply(200, runtime.tools())
                if not post and path == "/api/emergency/preflight":
                    return self.reply(200, runtime.emergency_preflight())
                if post and path == "/api/sessions/emergency":
                    data = self.body()
                    return self.reply(202, runtime.launch_emergency(
                        data.get("model_path") or None, data.get("gpu_layers", "auto")))
                if post and path == "/api/sessions":
                    return self.reply(201, runtime.create(self.body().get("profile")))
                parts = path.strip("/").split("/")
                if len(parts) in (3, 4) and parts[:2] == ["api", "sessions"]:
                    sid = parts[2]
                    if not post and len(parts) == 3:
                        after = max(0, int(parse_qs(parsed.query).get("after", ["0"])[0]))
                        return self.reply(200, runtime.view(sid, after))
                    if post and len(parts) == 4:
                        data = self.body()
                        action = parts[3]
                        if action == "profile":
                            return self.reply(200, runtime.select_profile(sid, data.get("profile")))
                        if action == "messages":
                            return self.reply(202, runtime.start(sid, data.get("text", "")))
                        if action == "resume":
                            return self.reply(202, runtime.start(sid, resume=True))
                        if action == "answer":
                            return self.reply(200, runtime.answer(sid, data.get("id"), data.get("choice")))
                        if action == "stop":
                            return self.reply(200, runtime.stop(sid))
                        if action == "clear":
                            return self.reply(200, runtime.clear_context(sid))
                self.reply(404, {"error": "Unknown route"})
            except KeyError:
                self.reply(404, {"error": "Unknown session"})
            except (ValueError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.reply(409, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.reply(500, {"error": "Service operation failed"})
    return Handler


def serve(cfg, profiles, state, host="127.0.0.1", port=9126):
    token = os.environ.get("HUGPY_SERVE_TOKEN", "")
    if host not in ("localhost", "127.0.0.1", "::1") and not token:
        raise ValueError("HUGPY_SERVE_TOKEN is required when listening beyond loopback")
    runtime = Runtime(cfg, state, profiles)
    server = ThreadingHTTPServer((host, port), handler(runtime, token))
    server.daemon_threads = True
    def stop(*_):
        runtime.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("hugpy-agent serve: http://%s:%d (workspace %s)" % (host, server.server_port, cfg.workspace), flush=True)
    try:
        server.serve_forever()
    finally:
        runtime.shutdown()
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hugpy Agent sessions for any configured model provider")
    parser.add_argument("--profiles", required=True, help="JSON model profiles; credentials are environment references")
    parser.add_argument("--state", default="~/.local/state/hugpy-agent-serve")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9126)
    parser.add_argument("--policy", choices=("ask", "readonly", "auto"), default="ask")
    args = parser.parse_args(argv)
    os.umask(0o077)
    cfg = load_config(overrides={"workspace": str(Path(args.workspace).expanduser().resolve()), "policy_mode": args.policy})
    if not Path(cfg.workspace).is_dir():
        parser.error("workspace does not exist")
    serve(cfg, args.profiles, args.state, args.host, args.port)
