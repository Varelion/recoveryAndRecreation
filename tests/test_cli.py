"""End to end through `vault.cli.main` with the fake guild in place of Discord."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from fakes import CHAT, GENERAL, GUILD, ROLE, FakeLive
from vault import cli, config


class _Link:
    def __init__(self, rest: FakeLive) -> None:
        self.rest = rest

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def live(tmp_path, monkeypatch):
    guild = FakeLive()
    monkeypatch.setattr(cli, "Link", lambda token: _Link(guild))
    monkeypatch.setenv(config.TOKEN_ENV, "token")
    monkeypatch.chdir(tmp_path)
    return guild


def _vault(monkeypatch, capsys, *argv: str) -> list[str]:
    monkeypatch.setattr(sys, "argv", ["vault", *argv])
    cli.main()
    return capsys.readouterr().out.splitlines()


def _report(lines: list[str]) -> dict:
    # Progress lines come first; the JSON summary is the rest.
    start = lines.index("{")
    return json.loads("\n".join(lines[start:]))


def test_backup_writes_dated_copies(live, monkeypatch, capsys):
    first = Path(_vault(monkeypatch, capsys, "backup", str(GUILD), "--out", "backups", "--no-media")[-1])
    second = Path(_vault(monkeypatch, capsys, "backup", str(GUILD), "--out", "backups", "--no-media")[-1])

    assert first != second
    assert first.parent == Path("backups") / str(GUILD)
    with sqlite3.connect(f"file:{second}?mode=ro", uri=True) as db:
        assert db.execute("SELECT count(*) FROM channels WHERE channel_id = ?", (CHAT,)).fetchone()[0] == 1


def test_restore_dry_run_then_live(live, monkeypatch, capsys):
    path = _vault(monkeypatch, capsys, "backup", str(GUILD), "--out", "backups", "--no-media")[-1]
    live.delete(GENERAL)
    live.drop_role(ROLE)

    dry = _report(_vault(monkeypatch, capsys, "restore", path, str(GUILD), "--dry-run"))
    assert dry["missing"] == {"roles": ["mod"], "channels": ["general"]}
    assert live.calls == []

    done = _report(_vault(monkeypatch, capsys, "restore", path, str(GUILD), "--structure-only"))
    assert done["channels"] == 1 and not done.get("messages")
    assert "general" in [c["name"] for c in live.channels_data]

    again = _report(_vault(monkeypatch, capsys, "restore", path, str(GUILD)))
    assert again["missing"] == {"roles": [], "channels": []}


def test_restore_rejects_missing_backup(live, monkeypatch, capsys):
    with pytest.raises(SystemExit, match="nope.db"):
        _vault(monkeypatch, capsys, "restore", "nope.db", str(GUILD))
