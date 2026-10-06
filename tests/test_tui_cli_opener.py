"""CLI opener (operator 2026-10-06): Shift + / or /cli opens a FRESH session of
the role's own CLI that resumes from the locus's handoff ledger ("no claude
resume. resume the way we do here"), as the serve's user, on the role's model."""
from hugpy_agent.serve_client.base import Session
from hugpy_agent.tui import loci


def local(host):
    return host in ("192.168.1.100", "localhost", "ae")


def test_claude_resumes_from_the_ledger_with_the_resume_command():
    argv, why = loci.cli_argv("claude", "hugpy", "claude-opus-5", "/srv/hugpy", "hugpy@192.168.1.100", "hugpy", local)
    assert why is None and argv[:2] == ["bash", "-lc"]
    cmd = argv[2]
    assert "export EXCHANGE_LOCUS=hugpy HUGPY_LOCUS=hugpy" in cmd and "cd /srv/hugpy" in cmd
    assert "exec abstract-claude launch --new --model claude-opus-5 '/resume hugpy'" in cmd
    assert "else exec claude --model claude-opus-5 '/resume hugpy'" in cmd
    assert "--resume" not in cmd                       # never the CLI's own transcript resume


def test_gpt_gets_the_same_ledger_steps_as_a_prompt():
    argv, _ = loci.cli_argv("gpt", "hugpy", "gpt-5.6-terra", "/srv/hugpy", "", "hugpy", local)
    cmd = argv[-1]
    assert "exec abstract-gpt launch -m gpt-5.6-terra" in cmd and "ledger_get" in cmd and "locus=hugpy" in cmd
    assert "codex resume" not in cmd


def test_no_locus_opens_a_plain_session():
    argv, _ = loci.cli_argv("claude", "", "", "", "", "u", local)
    assert "EXCHANGE_LOCUS" not in argv[-1] and "/resume" not in argv[-1]


def test_another_user_on_this_host_goes_over_ssh_as_the_serves_user():
    argv, _ = loci.cli_argv("claude", "hugpy", "", "/srv/hugpy", "hugpy@192.168.1.100", "solcatcher", local)
    assert argv[:3] == ["ssh", "-t", "hugpy@192.168.1.100"] and "/resume hugpy" in argv[-1]


def test_a_remote_host_goes_over_ssh_with_its_port():
    argv, _ = loci.cli_argv("claude", "x", "", "/w", "ubuntu@10.0.0.9", "ubuntu", local, ssh_port=2222)
    assert argv[:5] == ["ssh", "-t", "-p", "2222", "ubuntu@10.0.0.9"]


def test_unknown_backends_are_refused_with_a_reason():
    argv, why = loci.cli_argv("b", "x")
    assert argv is None and "no CLI to open" in why


def test_values_are_quoted():
    argv, _ = loci.cli_argv("claude", "lo cus", "m x", "/dir with space", "", "u", local)
    cmd = argv[-1]
    assert "cd '/dir with space'" in cmd and "--model 'm x'" in cmd and "'/resume lo cus'" in cmd


def test_the_generated_shell_parses():
    import subprocess
    for b in ("claude", "gpt"):
        for mode in ("ledger", "native"):
            argv, _ = loci.cli_argv(b, "hugpy", "m", "/tmp", "", "u", local, mode=mode, native_id="x")
            assert subprocess.run(["bash", "-n", "-c", argv[-1]]).returncode == 0


def test_a_local_registry_locus_keeps_its_login():
    rows = [{"locus": "hugpy", "status": "active",
             "pointer": {"serve_url": "http://127.0.0.1:9125", "ssh": {"user": "hugpy", "host": "192.168.1.100"}}}]
    entry = loci.registry_loci(rows, is_local=local)[0]
    assert entry["serve"] == "http://127.0.0.1:9125" and entry["login"] == "hugpy@192.168.1.100"


def test_session_rows_carry_the_serve_config():
    s = Session(id="cs-1", cwd="/w", effort="high", permission_mode="bypassPermissions")
    assert (s.cwd, s.effort, s.permission_mode) == ("/w", "high", "bypassPermissions")


def test_native_mode_forks_a_throwaway_copy_in_the_conversations_dir():
    argv, why = loci.cli_argv("claude", "hugpy", "", "/srv/hugpy/hugpy-station", "", "u", local,
                              mode="native", native_id="1a35e0a0")
    cmd = argv[-1]
    assert why is None and "abstract-claude launch --resume 1a35e0a0 --fork-session" in cmd
    assert "projects/*/1a35e0a0.jsonl" in cmd and "EXCHANGE_LOCUS=hugpy" in cmd
    g, _ = loci.cli_argv("gpt", "hugpy", "", "/srv/hugpy", "", "u", local, mode="native", native_id="01a1")
    assert "abstract-gpt launch fork 01a1" in g[-1] and "codex fork 01a1" in g[-1]
    assert loci.cli_argv("claude", mode="native")[0] is None       # no session yet: a reason


def test_native_mode_lands_in_the_recorded_dir(tmp_path):
    import os, subprocess
    proj = tmp_path / ".claude" / "projects" / "-x"
    proj.mkdir(parents=True)
    real = tmp_path / "real"
    real.mkdir()
    (proj / "sid1.jsonl").write_text('{"type":"user","cwd":"%s"}\n' % real)
    argv, _ = loci.cli_argv("claude", "", "", "/nonexistent", "", "u", local, mode="native", native_id="sid1")
    cmd = argv[-1].split("if command -v")[0] + "pwd"
    out = subprocess.run(["bash", "-c", cmd], env={**os.environ, "HOME": str(tmp_path)},
                         capture_output=True, text=True).stdout.strip()
    assert out == str(real)


def test_a_ledger_launch_never_wipes_the_serve_users_claude_dir():
    """abstract-claude launch wipes ~/.claude unless it resumes or gets --new/--keep:
    every abstract-claude launch the opener builds carries one of them."""
    import re
    for mode in ("ledger", "native"):
        argv, _ = loci.cli_argv("claude", "hugpy", "m", "/w", "", "u", local, mode=mode, native_id="x")
        for m in re.finditer(r"abstract-claude launch([^;]*)", argv[-1]):
            assert re.search(r"--new|--keep|--resume", m.group(1)), m.group(0)
