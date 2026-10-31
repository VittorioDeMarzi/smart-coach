"""The CLI's outer boundary: credentials, .env loading and login failures.

Every command must fail with a readable line, never a traceback (spec 11).
"""

from smart_coach import cli
from smart_coach.garmin_reader import GarminAuthError


def _plan(tmp_path) -> str:
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n**Goal:** Test\n\n## 2026-W40\n\n"
                    "- 2026-09-28 | Easy | 8.0 km | easy\n")
    return str(path)


def test_snapshot_without_credentials_reports_instead_of_crashing(monkeypatch, capsys):
    """`snapshot` is the first command anyone runs; it used to bypass the handler."""
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)

    code = cli.main(["snapshot"])

    assert code == 1
    assert "GARMIN_API_KEY" in capsys.readouterr().err


def test_a_refused_garmin_key_is_reported_not_raised(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "DEFAULT_PLAN", _plan(tmp_path))

    def _refuse():
        raise GarminAuthError("Garmin refused GARMIN_API_KEY (HTTP 401)")

    monkeypatch.setattr(cli, "build_client", _refuse)

    code = cli.main(["validate", "--week", "40"])

    assert code == 1
    assert "GARMIN_API_KEY" in capsys.readouterr().err


def test_dotenv_is_loaded_before_reading_the_environment(monkeypatch, capsys, tmp_path):
    """A .env file must actually reach os.environ, or documenting it is a lie."""
    env = tmp_path / ".env"
    env.write_text("GARMIN_API_KEY=from-the-env-file\n")
    monkeypatch.setattr(cli, "ENV_FILE", str(env))

    seen = {}

    def _capture():
        import os
        seen["key"] = os.environ.get("GARMIN_API_KEY")
        raise GarminAuthError("stop here")

    monkeypatch.setattr(cli, "build_client", _capture)
    monkeypatch.setattr(cli, "DEFAULT_PLAN", _plan(tmp_path))

    cli.main(["validate", "--week", "40"])

    assert seen["key"] == "from-the-env-file"
