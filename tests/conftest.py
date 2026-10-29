"""What every test shares.

A download is checked against the addresses its host resolves to before it
is asked for (``feeds._check_hop``), and the tests' stand-in hosts sit under
the names reserved for testing (RFC 2606, RFC 6761), which no resolver
answers. Offline, as every download test is, they resolve here to one public
address; any other name, and every literal address, goes to the real
resolver, and a test that judges a resolution patches its own.
"""

import socket

import pytest

# A public address (example.com's, at the time of writing); nothing connects.
PUBLIC = "93.184.215.14"
RESERVED = (".test", ".invalid", ".example")

_real_getaddrinfo = socket.getaddrinfo


def _getaddrinfo(host, port, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else str(host or "")
    if name.rstrip(".").lower().endswith(RESERVED):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (PUBLIC, port or 0))]
    return _real_getaddrinfo(host, port, *args, **kwargs)


@pytest.fixture(autouse=True)
def reserved_names_resolve_publicly(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
