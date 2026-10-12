import discord
import pytest

from vault import config
from vault.api.rest import Rest
from vault.services.media import Downloader
from vault.services.progress import Progress

HTTP_BAD_REQUEST = 400


class BadAssetHttp:
    """HTTPClient stand-in: CDN refuses the first URL with 400, serves the rest."""

    async def get_from_cdn(self, url):
        if url == "bad":
            raise discord.HTTPException(FakeResponse(HTTP_BAD_REQUEST), "failed to get asset")

        return b"img"


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Bad Request"


class FakeStore:
    def __init__(self) -> None:
        self.saved = []

    async def pending_media(self, guild_id):
        return [
            {"attachment_id": 1, "filename": "a.png", "url": "bad"},
            {"attachment_id": 2, "filename": "b.png", "url": "good"},
        ]

    async def put_media(self, attachment_id, sha, path, size):
        self.saved.append(attachment_id)

    async def commit(self):
        pass


async def test_bad_asset_skipped(tmp_path):
    # One refused file must not abort a multi-hour export.
    store = FakeStore()
    downloader = Downloader(Rest(BadAssetHttp()), store, tmp_path, workers=2)

    assert await downloader.run(1, Progress("export")) == 1
    assert store.saved == [2]


HTTP_GATEWAY_TIMEOUT = 504


class TimeoutHttp:
    """HTTPClient stand-in: CDN answers 504 `drops` times for "bad", then serves."""

    def __init__(self, drops: int) -> None:
        self.drops = drops
        self.calls = 0

    async def get_from_cdn(self, url):
        if url != "bad":
            return b"img"

        self.calls += 1
        if self.calls <= self.drops:
            raise discord.HTTPException(FakeResponse(HTTP_GATEWAY_TIMEOUT), "failed to get asset")

        return b"img"


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(config, "NET_RETRY_DELAY", 0)


async def test_cdn_timeout_retried(tmp_path, no_wait):
    store = FakeStore()
    downloader = Downloader(Rest(TimeoutHttp(drops=2)), store, tmp_path, workers=2)

    assert await downloader.run(1, Progress("export")) == 2
    assert sorted(store.saved) == [1, 2]


async def test_cdn_outage_skipped(tmp_path, no_wait):
    # File stays pending; the next export retries it.
    store = FakeStore()
    http = TimeoutHttp(drops=config.NET_RETRIES + 1)
    downloader = Downloader(Rest(http), store, tmp_path, workers=2)

    assert await downloader.run(1, Progress("export")) == 1
    assert store.saved == [2]
