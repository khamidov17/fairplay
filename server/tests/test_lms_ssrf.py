"""Audit 2026-10-12 L-01: the LMS address can never reach a private network."""
import asyncio
import socket

import httpx
import pytest

from app.lms import base
from app.lms.base import LmsError, literal_ip, normalize_base_url, resolve_public


@pytest.mark.parametrize("host", ["127.1", "0x7f000001", "2130706433", "0177.0.0.1", "127.0.0.1",
                                  "10.0.0.5", "169.254.169.254", "[::1]", "[::ffff:127.0.0.1]",
                                  "0.0.0.0", "localhost", "foo.localhost", "100.64.0.1"])
def test_private_addresses_in_any_notation_are_refused(host):
    with pytest.raises(LmsError, match="private network"):
        normalize_base_url(f"https://{host}/")


def test_public_names_and_addresses_pass():
    assert normalize_base_url("school.instructure.com/api/v1") == "https://school.instructure.com"
    assert str(literal_ip("2130706433")) == "127.0.0.1"
    assert literal_ip("school.instructure.com") is None


def _fake_dns(monkeypatch, answer):
    async def getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answer]
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    return loop


def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    loop = _fake_dns(monkeypatch, ["93.184.216.34", "10.1.2.3"])   # one bad answer is enough
    with pytest.raises(LmsError, match="private network"):
        loop.run_until_complete(resolve_public("evil.example", 443))
    loop.close()


def test_a_public_name_resolves_to_the_checked_address(monkeypatch):
    loop = _fake_dns(monkeypatch, ["93.184.216.34"])
    assert loop.run_until_complete(resolve_public("school.example", 443)) == "93.184.216.34"
    loop.close()


def test_the_real_transport_refuses_rebinding_to_loopback(monkeypatch):
    """End to end through httpx: the guarded backend resolves, checks and refuses."""
    async def go():
        loop = asyncio.get_running_loop()

        async def getaddrinfo(host, port, type=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
        async with httpx.AsyncClient(transport=base.public_only_transport()) as c:
            with pytest.raises(LmsError, match="private network"):
                await c.get("https://rebind.example/api/v1/users/self")
    asyncio.run(go())


def test_providers_use_the_guarded_transport_by_default():
    from app.lms import make_provider
    base.set_transport_factory(None)
    p = make_provider("canvas", "https://school.instructure.com", "tok")
    assert isinstance(p._client._transport, httpx.AsyncHTTPTransport)
    assert isinstance(p._client._transport._pool._network_backend, base._PublicOnlyBackend)
    asyncio.run(p.aclose())


def test_optional_lms_host_allow_list(monkeypatch):
    from app.routes_lms import lms_host_allowed
    assert lms_host_allowed("https://anything.example")
    monkeypatch.setenv("FAIRPLAY_LMS_HOSTS", "instructure.com, moodle.dku.edu")
    assert lms_host_allowed("https://dku.instructure.com")
    assert lms_host_allowed("https://moodle.dku.edu")
    assert not lms_host_allowed("https://evil-instructure.com")
    assert not lms_host_allowed("https://attacker.example")
