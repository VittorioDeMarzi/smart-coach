"""The CLI's outer boundary: credentials, .env loading and login failures.

Every command must fail with a readable line, never a traceback (spec 11).
"""

import pytest

from smart_coach import cli
from smart_coach.garmin_reader import GarminAuthError


def _plan(tmp_path) -> str:
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n**Goal:** Test\n\n## 2026-W40\n\n"
                    "- 2026-09-28 | Easy | 8.0 km | easy\n")
    return str(path)


def test_snapshot_without_credentials_reports_instead_of_crashing(monkeypatch, capsys):
    """`snapshot` is the first command anyone runs; it used to bypass the handler."""
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)

    code = cli.main(["snapshot"])

    assert code == 1
    assert "GARMIN_EMAIL" in capsys.readouterr().err


def test_a_failed_garmin_login_is_reported_not_raised(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "DEFAULT_PLAN", _plan(tmp_path))

    def _refuse(prompt_mfa=None):
        raise GarminAuthError("login refused by Garmin: an MFA code is needed")

    monkeypatch.setattr(cli, "build_client", _refuse)

    code = cli.main(["validate", "--week", "40"])

    assert code == 1
    assert "MFA" in capsys.readouterr().err


def test_dotenv_is_loaded_before_reading_the_environment(monkeypatch, capsys, tmp_path):
    """A .env file must actually reach os.environ, or documenting it is a lie."""
    env = tmp_path / ".env"
    env.write_text("GARMIN_EMAIL=someone@example.com\nGARMIN_PASSWORD=secret\n")
    monkeypatch.setattr(cli, "ENV_FILE", str(env))

    seen = {}

    def _capture(prompt_mfa=None):
        import os
        seen["email"] = os.environ.get("GARMIN_EMAIL")
        raise GarminAuthError("stop here")

    monkeypatch.setattr(cli, "build_client", _capture)
    monkeypatch.setattr(cli, "DEFAULT_PLAN", _plan(tmp_path))

    cli.main(["validate", "--week", "40"])

    assert seen["email"] == "someone@example.com"


def test_build_client_wraps_login_failures_in_a_domain_error(monkeypatch):
    """A specific error type at the boundary, not whatever garminconnect happens to raise."""
    import smart_coach.garmin_reader as reader

    monkeypatch.setenv("GARMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("GARMIN_PASSWORD", "x")
    monkeypatch.setattr(reader, "_garmin_login",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ValueError("boom from the library")))

    with pytest.raises(GarminAuthError) as caught:
        reader.build_client()
    assert "boom from the library" in str(caught.value)


# --- MFA: the code is the athlete's to type, and only once ---------------------------

def test_build_client_passes_the_mfa_prompt_and_token_cache(monkeypatch):
    import smart_coach.garmin_reader as reader

    monkeypatch.setenv("GARMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("GARMIN_PASSWORD", "x")
    seen = {}

    def _fake(email, password, prompt_mfa, tokenstore):
        seen.update(email=email, prompt_mfa=prompt_mfa, tokenstore=tokenstore)
        return "client"

    monkeypatch.setattr(reader, "_garmin_login", _fake)

    def _prompt():
        return "123456"

    assert reader.build_client(_prompt) == "client"
    assert seen["prompt_mfa"] is _prompt
    assert seen["tokenstore"] == reader.TOKEN_STORE, (
        "without a token store the MFA code would be asked on every single run")


def test_the_mfa_prompt_refuses_to_run_without_a_terminal(monkeypatch):
    """A non-interactive run must fail with an instruction, never hang on input()."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    with pytest.raises(GarminAuthError) as caught:
        cli.mfa_prompt()
    assert "terminal" in str(caught.value)


def test_the_mfa_prompt_reads_the_code_from_the_terminal(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "  123456 ")

    assert cli.mfa_prompt() == "123456"
