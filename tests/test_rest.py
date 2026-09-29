import aiohttp
import pytest

from vault import config
from vault.api.rest import Rest

WIN_NETNAME_DELETED = 64


class FlakyHttp:
    """HTTPClient stand-in: drops the connection `drops` times, then answers."""

    def __init__(self, drops: int) -> None:
        self.drops = drops
        self.calls = 0

    async def request(self, route, **kwargs):
        self.calls += 1
        if self.calls <= self.drops:
            raise aiohttp.ClientOSError(WIN_NETNAME_DELETED, "The specified network name is no longer available")

        return []


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(config, "NET_RETRY_DELAY", 0)


async def test_read_survives_network_drop():
    http = FlakyHttp(drops=2)

    assert await Rest(http).channels(1) == []
    assert http.calls == 3


async def test_read_gives_up_eventually():
    http = FlakyHttp(drops=config.NET_RETRIES + 1)

    with pytest.raises(aiohttp.ClientOSError):
        await Rest(http).channels(1)


async def test_write_not_retried():
    # Server may have applied it; a resend could duplicate.
    http = FlakyHttp(drops=1)

    with pytest.raises(aiohttp.ClientOSError):
        await Rest(http).send_message(1, {"content": "x"})

    assert http.calls == 1
