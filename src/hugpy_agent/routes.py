"""Fleet arm Flask blueprint — ``fleet_bp``.

Requires the ``hugpy-agent[fleet]`` extra (abstract_flask + flask); the base
package never imports this module. A host discovers the blueprint by the
``hugpy.arms`` entry point (``hugpy_agent.routes:fleet_bp``) or by passing this
module as ``routes=`` to ``abstract_flask.get_Flask_app`` (it scans for objects
named ``*_bp``). Every route is ``/<category>/<route>`` from ``get_toolset()``;
mount the blueprint under ``/api`` so the UI's ``/api/agent/*`` calls land here.
"""
from __future__ import annotations

from abstract_flask import Blueprint
from abstract_flask.generator.generateFlaskRoute import register_categories

from .toolset import get_toolset

fleet_bp = Blueprint("fleet_bp", __name__)
register_categories(fleet_bp, get_toolset())
