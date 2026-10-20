"""Standalone Fleet service: the toolset API under /api + the built UI at /fleet/.

Mirrors the reference ``wsgi.py``: ``get_Flask_app(routes=hugpy_agent.routes)``
auto-discovers ``fleet_bp`` and mounts it under ``/api``, so the UI's
``/api/agent/*`` calls reach the toolset. The UI single-page app is served from
the packaged ``hugpy_agent/ui_dist`` at ``/fleet/`` (SPA fallback), and ``/``
redirects to ``/fleet/``.

Config (read at serve time; nothing opens a socket on import):
  HUGPY_AGENT_CONSOLE_HOST   default 127.0.0.1
  HUGPY_AGENT_CONSOLE_PORT   default 7342
  /etc/hugpy/agent.env       optional KEY=VALUE file (does not override the env)

Requires the ``hugpy-agent[console]`` extra (abstract_flask + flask). The unit
runs this as ``hugpy-agent console-serve``.
"""
from __future__ import annotations

import os

from .imports import constants as C
from .imports.functions import parse_env_file


def _load_env_file() -> None:
    """Fold /etc/hugpy/agent.env into the environment (process env wins)."""
    for k, v in parse_env_file(C.AGENT_ENV_FILE).items():
        os.environ.setdefault(k, v)


def create_app():
    """Build the Flask app: /api/agent/* toolset routes + /fleet/ UI."""
    _load_env_file()
    from abstract_flask import get_Flask_app
    from flask import jsonify, request

    from . import routes as _routes
    from .fleet import install_links as _links
    from .fleet.console_artifacts import package_catalog
    app = get_Flask_app(name="hugpy-agent-console", routes=_routes,
                        url_prefix="api", allowed_origins=None)

    # The Core Console's API tab uses these REST URLs. The generated toolset
    # routes remain available for agent callers, but have underscore names and
    # cannot implement DELETE or path parameters for the UI.
    @app.route("/api/agent/install-links", methods=["GET", "POST"])
    @app.route("/api/agent/console/install-links", methods=["POST"])
    def _install_links():
        if request.method == "GET":
            return jsonify(_links.list_links())
        body = request.get_json(silent=True) or {}
        if request.path.endswith("/console/install-links"):
            body["kind"] = "console"
        try:
            return jsonify(_links.mint(**{k: body[k] for k in (
                "label", "scopes", "link_ttl_s", "max_uses", "key_expires_at", "kind") if k in body}))
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.route("/api/agent/install-links/packages", methods=["GET"])
    def _installer_packages():
        return jsonify({"packages": package_catalog()})

    @app.route("/api/agent/install-links/prune", methods=["POST"])
    def _prune_install_links():
        return jsonify(_links.prune())

    @app.route("/api/agent/install-links/<link_id>", methods=["DELETE"])
    def _delete_install_link(link_id):
        try:
            result = (_links.remove if request.args.get("purge") == "1" else _links.revoke)(link_id)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(result), (200 if result["found"] else 404)

    # 2026-10-20 (operator): the agent is a BACKEND — "it doesn't need to be a
    # frontend anything". No /fleet page is served; the arm is its API only
    # (/api/agent/*, install links). The console's Station tab is the harness.
    @app.route("/")
    def _root():
        return jsonify({"service": "hugpy-agent", "arm": C.ARM_NAME, "api": "/api/agent/",
                        "note": "backend only; the Core console's Station tab is the harness"})

    @app.route("/fleet", defaults={"path": ""})
    @app.route("/fleet/", defaults={"path": ""})
    @app.route("/fleet/<path:path>")
    def _fleet_retired(path):
        return jsonify({"error": "the /fleet page was retired; hugpy-agent is a backend (/api/agent/*). "
                                 "Sessions live in the Core console's Station tab."}), 410

    return app


def main(argv=None) -> int:
    _load_env_file()
    host = os.environ.get(C.ENV_CONSOLE_HOST) or C.DEFAULT_CONSOLE_HOST
    try:
        port = int(os.environ.get(C.ENV_CONSOLE_PORT) or C.DEFAULT_CONSOLE_PORT)
    except (TypeError, ValueError):
        port = C.DEFAULT_CONSOLE_PORT
    app = create_app()
    app.run(host=host, port=port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
