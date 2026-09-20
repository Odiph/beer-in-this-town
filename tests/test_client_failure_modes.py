"""Read-side failure modes that used to degrade quietly or sleep for hours.

Nothing here touches the network: the transport is stubbed and time.sleep is
neutered, so the pacing logic can be exercised without paying for it.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.http_client import (
    PoliteClient,
    RateLimitTripped,
    TransportUnavailable,
)


@pytest.fixture
def nosleep(monkeypatch):
    from beer_in_this_town import http_client
    monkeypatch.setattr(http_client.time, "sleep", lambda _: None)


@pytest.mark.unit
def test_a_blocked_robots_fetch_is_not_read_as_permission(nosleep, monkeypatch):
    """A 403 on robots.txt means a block is already in progress.

    It was swallowed by a bare `except Exception: return False`, so the run
    read "robots does not forbid this" and carried on requesting into the
    block -- the one move `http_client`'s own 403 rule says never to make.
    """
    with PoliteClient(Settings()) as client:
        def blocked(*a, **kw):
            raise RateLimitTripped("403 from untappd.com")

        monkeypatch.setattr(client, "get", blocked)
        with pytest.raises(RateLimitTripped):
            client.robots_disallows_scraping()


@pytest.mark.unit
def test_a_missing_robots_file_still_means_no_prohibition(nosleep, monkeypatch):
    """Absence is not a rule. Only a block is treated as one."""
    with PoliteClient(Settings()) as client:
        def gone(*a, **kw):
            raise ValueError("404 not found")

        monkeypatch.setattr(client, "get", gone)
        assert client.robots_disallows_scraping() is False


@pytest.mark.unit
def test_repeated_transport_failures_stop_the_run(nosleep, monkeypatch):
    """A dead network made `run` sleep, not stop.

    Each venue retried 3x over 60/180/600s and the caller swallowed the
    result per-venue, so 100 venues meant ~23 hours of sleeping before the
    corpus gate finally failed. There is a trip for consecutive 429s; there
    was none for a transport that is simply gone.
    """
    import httpx

    from beer_in_this_town import http_client

    s = Settings(max_retries=1, backoff_ladder_s=(0,))
    with PoliteClient(s) as client:
        monkeypatch.setattr(
            client._client, "get",
            lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")))
        for _ in range(http_client.MAX_CONSECUTIVE_TRANSPORT_ERRORS - 1):
            with pytest.raises(Exception) as caught:
                client.get("https://untappd.com/v/x/1", use_cache=False)
            assert not isinstance(caught.value, TransportUnavailable)
        with pytest.raises(TransportUnavailable):
            client.get("https://untappd.com/v/x/2", use_cache=False)


@pytest.mark.unit
def test_one_success_clears_the_transport_counter(nosleep, monkeypatch):
    """A flaky connection is not a dead one."""
    import httpx

    from beer_in_this_town import http_client

    s = Settings(max_retries=1, backoff_ladder_s=(0,))
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        text = "<html></html>"
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

    with PoliteClient(s) as client:
        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= http_client.MAX_CONSECUTIVE_TRANSPORT_ERRORS - 1:
                raise httpx.ConnectError("down")
            return _Resp()

        monkeypatch.setattr(client._client, "get", flaky)
        for _ in range(http_client.MAX_CONSECUTIVE_TRANSPORT_ERRORS - 1):
            with pytest.raises(RuntimeError):
                client.get("https://untappd.com/v/x/1", use_cache=False)
        assert client.get("https://untappd.com/v/x/2", use_cache=False)
        assert client._consecutive_transport_errors == 0
