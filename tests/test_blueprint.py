from pathlib import Path

import pytest

from test_builder import DOCS, MANIFEST, blueprint
from vault.config import CONTENT_LIMIT, ChannelType
from vault.services.blueprint import BlueprintError, Permission, load
from vault.services.builder import _chunks, _size

COURTS_UNBOUND = Path(__file__).parent.parent / "blueprints" / "courts_unbound"
FENCE = "```"


def test_loads_manifest(tmp_path):
    layout = blueprint(tmp_path)

    assert layout.name == "Courts Unbound"
    assert [r.name for r in layout.roles] == ["Staff", "Night Court"]
    assert layout.roles[0].permissions is None

    public = {ow.role: ow for ow in layout.presets["public"]}
    assert public["@everyone"].allow == Permission.VIEW
    assert public["@everyone"].deny == Permission.SEND

    channels = {c.id: c for c in layout.channels()}
    assert channels["lounge"].kind is ChannelType.VOICE
    assert channels["claims"].threads[0].doc.text == DOCS["docs/claims.md"]


def test_reports_every_problem_at_once(tmp_path):
    broken = MANIFEST.replace('access = "hall"', 'access = "nowhere"') \
        .replace('"Staff" = { allow = ["send"] }', '"Ghost" = { allow = ["fly"] }') \
        .replace('name = "rules"', 'name = "Rules Channel"') \
        .replace('id = "lounge"', 'id = "rules"') \
        .replace('docs/claims.md', 'docs/missing.md')

    with pytest.raises(BlueprintError) as error:
        blueprint(tmp_path, broken, DOCS | {"docs/rules.md": "See {#nowhere} and {@Nobody}."})

    report = str(error.value)
    for expected in ("unknown access preset 'nowhere'", "unknown role 'Ghost'", "unknown permission 'fly'",
                     "must be lowercase", "duplicate id", "'docs/missing.md' not found",
                     "unknown channel {#nowhere}", "unknown role {@Nobody}"):
        assert expected in report


def test_grant_needs_a_grant_preset(tmp_path):
    broken = MANIFEST.replace('grant = ["Night Court"]', 'grant = ["Night Court"]\naccess = "public"')

    with pytest.raises(BlueprintError, match="grant needs a preset"):
        blueprint(tmp_path, broken)


def test_courts_unbound_blueprint_is_valid():
    layout = load(COURTS_UNBOUND)

    # Every message the build would post fits Discord and keeps code fences balanced.
    for channel in layout.channels():
        for doc in channel.docs + tuple(t.doc for t in channel.threads):
            for part in _chunks(doc.text):
                assert _size(part) <= CONTENT_LIMIT, doc.name
                assert part.count(FENCE) % 2 == 0, f"{doc.name}: code fence split across messages"
