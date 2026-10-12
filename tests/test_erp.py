import pytest

from fakes import GUILD, TEXT, USER, message
from vault.services.erp import Category, scan, score
from vault.store import Store


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def test_lone_suggestive_ignored():
    report = scan(USER, ["she kissed his lips", "a warm bed"])

    assert report.flagged == 0
    assert report.score == 0


def test_categories_weighted():
    report = scan(USER, ["slowly, deeper", "she moans... no, MOAN", "ftb", "*time skip*", "hello"])

    assert report.hits[Category.SUGGESTIVE] == {"slowly": 1, "deeper": 1}
    assert report.hits[Category.EXPLICIT] == {"moan": 1}
    assert report.hits[Category.MARKER] == {"ftb": 1, "*time skip*": 1}
    assert report.flagged == 4
    assert report.points == 2 * 1 + 1 * 3 + 2 * 2
    assert report.score == report.points * 100 / 5


def test_long_terms_match_as_stems():
    report = scan(USER, ["penetrating, ejaculated, clitoris"])

    assert report.hits[Category.EXPLICIT] == {"penetrat": 1, "ejaculat": 1, "clitoris": 1}


def test_whole_words_only():
    assert scan(USER, ["sussex lickety bedrock"]).flagged == 0


async def test_lookup_by_id_and_name(store):
    await store.put_messages(GUILD, [message(1, TEXT, "nsfw"), message(2, TEXT, "hi")])

    by_id = await score(store, str(USER))
    by_name = await score(store, "ANN")

    assert by_id.summary() == by_name.summary()
    assert by_id.messages == 2
    assert by_id.hits[Category.MARKER] == {"nsfw": 1}
    assert await score(store, "nobody") is None
