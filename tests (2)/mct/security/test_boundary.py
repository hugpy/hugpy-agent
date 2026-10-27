"""Security boundary: A cannot bypass B; fail-closed everywhere (design §22.1)."""
from hugpy_agent.mct import fake_a


def test_bypass_attempts_all_fail_closed(session):
    prog = fake_a.try_bypass()
    session.submit("attempt bypass", prog)
    codes = prog.violations
    assert codes[0] == "mct.protocol"    # host path is not a pointer (invariant 5)
    assert codes[1] == "mct.isolation"   # cross-session pointer (adversarial case 5)
    assert codes[2] == "denied"          # cross-session pull (invariant 12)


def test_client_surface_exposes_no_store_or_ledger(session):
    captured = {}

    def spy(client):
        captured["client"] = client
        client.read_operator_turn()
        client.respond("ok")

    session.submit("hi", spy)
    client = captured["client"]
    public = [a for a in dir(client) if not a.startswith("_")]
    # A holds only brokered operations — no store, ledger, filesystem, or roots.
    assert set(public) <= {"resolve", "open_manifest", "read_operator_turn",
                           "submit_pull", "respond", "respond_stream",
                           "session_id", "turn_id", "epoch", "manifest_pointer"}


def test_terminal_escapes_in_response_are_rejected(session):
    def evil(client):
        client.read_operator_turn()
        client.respond("clear screen \x1b[2J now")  # adversarial case 9

    r = session.submit("hi", evil)
    # validation fails -> discarded, not rendered; B does not invent an answer
    assert not r.rendered
    assert session.server.printed == []


def test_b_only_answering_disabled_by_default(broker):
    assert broker.config.allow_b_only_answer is False  # decision §26, invariant 10
