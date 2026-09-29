"""Precise token/cost accounting for A (design cache model, §12)."""
import json

from hugpy_agent.mct.tokens import summary_from_result


def test_summary_math_5min_cache():
    result = {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 4000,
                        "cache_read_input_tokens": 100000, "output_tokens": 500,
                        "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                           "ephemeral_5m_input_tokens": 4000}},
              "total_cost_usd": 0.06}
    s = summary_from_result(result)
    assert s["input"] == 10 and s["cache_read"] == 100000 and s["output"] == 500
    assert s["relayed_input"] == 104010          # 10 + 4000 + 100000
    # billed input-equiv = 10 + 1.25*4000 + 0.10*100000 = 15010
    assert abs(s["billed_input_equiv"] - 15010) < 1
    assert s["cost_usd"] == 0.06


def test_1hour_cache_write_multiplier():
    result = {"usage": {"input_tokens": 0, "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 0, "output_tokens": 0,
                        "cache_creation": {"ephemeral_1h_input_tokens": 1000,
                                           "ephemeral_5m_input_tokens": 0}},
              "total_cost_usd": 0.0}
    s = summary_from_result(result)
    assert abs(s["billed_input_equiv"] - 2000) < 1     # 1h cache write billed at 2.0x


def test_report_and_render_aggregate_from_transcripts(broker):
    sess = broker.session(broker.open_session("t"))
    for turn, (cr, cost) in {"t_000000": (100000, 0.06), "t_000001": (120000, 0.09)}.items():
        result = {"type": "result", "total_cost_usd": cost,
                  "usage": {"input_tokens": 10, "cache_creation_input_tokens": 4000,
                            "cache_read_input_tokens": cr, "output_tokens": 500}}
        raw = json.dumps({"type": "system"}) + "\n" + json.dumps(result) + "\n"
        broker.store.commit(sess.session_id, raw.encode(), media_type="application/x-ndjson",
                            kind="a_transcript", provenance={"turn_id": turn})
    rep = broker.tokens.report(sess.session_id)
    assert rep["turns"] == 2
    assert rep["cache_read"] == 220000
    assert abs(rep["cost_usd"] - 0.15) < 1e-6
    assert rep["cached_pct"] > 90                       # most context served from cache
    assert 0 < rep["input_cost_saved_by_cache_pct"] < 100
    text = broker.tokens.render(sess.session_id)
    assert "TOTAL" in text and "caching saved" in text


def test_no_transcripts_is_empty(broker):
    sess = broker.session(broker.open_session("t"))
    rep = broker.tokens.report(sess.session_id)
    assert rep["turns"] == 0 and rep["cost_usd"] == 0.0
