"""Phase 5 shadow-evaluation acceptance (design §22 Phase 5). Offline, deterministic.

Runs the suite once (module-scoped) and asserts the exit-condition thresholds.
"""
import pytest

from hugpy_agent.mct.evaluation import ShadowEvaluator


@pytest.fixture(scope="module")
def report():
    # Lighter scaling lengths keep the test fast; tools/phase5_eval.py uses the full set.
    return ShadowEvaluator(use_model=True).run_suite_offline(scaling_lengths=(50, 200, 400))


def test_material_token_savings_on_large_context(report):
    # log + config sources: bounded excerpts instead of bulk ingestion (§1.2)
    assert report["summary"]["mean_reduction_large_context"] >= 0.5


def test_bounded_working_set(report):
    scaling = report["scaling"]
    assert scaling[-1]["token_reduction"] >= 0.5           # savings grow with history
    # MCT input plateaus while baseline grows unbounded (design §0)
    assert scaling[-1]["mct_tokens"] <= scaling[1]["mct_tokens"] * 1.2
    assert scaling[-1]["baseline_tokens"] > 3 * scaling[-1]["mct_tokens"]


def test_curation_never_loses_needed_evidence(report):
    assert report["summary"]["omission_errors"] == 0       # zero omission errors


def test_pull_recovery(report):
    # the two source tasks omit evidence from initial context; A recovers via pull
    assert report["summary"]["pull_recovered"] >= 2


def test_latest_instruction_adherence(report):
    assert report["summary"]["adherence_pass"] is True


def test_selection_recall_complete(report):
    assert report["summary"]["selection_recall"] == 1.0    # all relevant retrieved


def test_fault_recovery(report):
    # epoch reset + source change -> re-snapshot + serve new content, no stale residency
    assert report["summary"]["fault_recovered"] is True
