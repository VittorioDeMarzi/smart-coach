"""Test isolation.

The CLI loads credentials from .env, so without this every test run on a developer
machine would read real credentials and attempt real Garmin logins. The suite must be
hermetic: no real secrets, no network.
"""

import pytest

from smart_coach import cli

CREDENTIALS = ("GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMIN_TOKEN_STORE",
               "INTERVALS_ATHLETE_ID", "INTERVALS_API_KEY",
               "GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE")


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch, tmp_path):
    """Point the CLI at an empty .env and clear any inherited credentials.

    A test that needs a .env sets `cli.ENV_FILE` to its own file.
    """
    monkeypatch.setattr(cli, "ENV_FILE", str(tmp_path / "absent.env"))
    # The developer's own athlete file must not leak into the suite either: a data gap
    # they declared would change which regime the tests run in.
    monkeypatch.setattr(cli, "DEFAULT_ATHLETE", str(tmp_path / "absent-athlete.toml"))
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
