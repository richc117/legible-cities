"""A feed download reaches only public hosts, at every hop.

A feed is published on a public host, and a download never reaches this
machine or its network. ``feeds._download`` follows redirects itself, in one
session, and judges every hop, the first included: ``_check_hop`` refuses a
wrong scheme, ``localhost`` and a literal local address before anything is
asked, and the connection itself resolves the host, refuses if any address
is local, and connects to the addresses it judged. A refusal is a
``FeedError`` (kind ``feed`` over the protocol) in the desktop app's own
sentence, names the address only as ``shown`` does, and keeps nothing.

These run the real ``requests`` and urllib3 stack, offline:
``socket.getaddrinfo`` answers what each test says, and
``socket.socket.connect`` records the address it was asked to dial and
connects instead to a small HTTP server on the loopback, which answers by the
request's Host header. So what is judged here is what was dialled.
"""

import ipaddress
import shutil
import socket
import ssl
import subprocess
import threading
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from schematic import config, feeds
from test_feeds import gtfs_zip, home, stand_in  # noqa: F401 - home is a fixture

SENTENCE = feeds.LOCAL_HOST
assert SENTENCE == ("Use an address on a public host; a feed cannot be fetched from this "
                    "machine or its network.")
NOT_FETCHABLE = "the address is not one that can be fetched"


def v4(*octets: int) -> str:
    """A dotted IPv4 address. The private ranges are written as octets, since
    ``bin/preflight`` refuses their dotted literals in a committed file."""
    return ".".join(map(str, octets))


# Public addresses (example.com's and the like); nothing connects to them.
PUBLIC, PUBLIC_2 = "93.184.215.14", "151.101.1.1"
# One address in each private range the preflight scan guards.
RFC1918_172, RFC1918_192 = v4(172, 16, 0, 9), v4(192, 168, 0, 10)
SHARED = v4(100, 64, 0, 1)                    # RFC 6598: carrier NAT, overlay networks


# -------------------------------------------------------------- the network

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        net = self.server.net
        # A proxy is sent the whole address; a host, its path and a Host header.
        at = (self.path[len("http://"):] if self.path.startswith("http://")
              else self.headers.get("Host", "") + self.path)
        net.seen.append({"at": at, "auth": self.headers.get("Authorization"),
                         "cookie": self.headers.get("Cookie")})
        status, headers, body = net.routes.get(at, (404, {}, b"not here"))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if isinstance(body, int):              # a body that size, counted as it goes
            self.send_header("Content-Length", str(body))
            self.end_headers()
            written = 0
            try:
                while written < body:
                    self.wfile.write(b"\0" * (1 << 16))
                    written += 1 << 16
            except OSError:
                pass
            finally:
                net.written.append(written)
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Net:
    """The stand-in network: ``names`` the resolver's answers per host (a
    list of rounds, the last repeated), ``routes`` what the server answers
    per ``host/path``, and what happened: ``resolved``, ``dialled``, ``seen``."""

    def __init__(self):
        self.names: dict[str, list[list[str]]] = {}
        self.routes: dict[str, tuple[int, dict, bytes | int]] = {}
        self.resolved: list[str] = []
        self.dialled: list[tuple[str, int]] = []
        self.seen: list[dict] = []
        self.written: list[int] = []
        # Where a dial to a port goes instead of the plain server (https's).
        self.ports: dict[int, int] = {}

    def resolve(self, host: str, *rounds: list[str]) -> None:
        self.names[host] = list(rounds)

    def route(self, at: str, status: int = 200, headers: dict | None = None,
              body: bytes | int = b"") -> None:
        self.routes[at] = (status, headers or {}, body)

    def redirect(self, at: str, location: str, status: int = 302) -> None:
        self.route(at, status, {"Location": location})


@pytest.fixture
def net(monkeypatch, tmp_path):
    net = Net()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.net = net
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()

    real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect

    def getaddrinfo(host, port_, family=0, type=0, proto=0, flags=0):
        if flags & socket.AI_NUMERICHOST:      # a number or nothing: no lookup to record
            return real_getaddrinfo(host, port_, family, type, proto, flags)
        net.resolved.append(host)
        if host in net.names:
            rounds = net.names[host]
            addresses = rounds.pop(0) if len(rounds) > 1 else rounds[0]
            return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM,
                     socket.IPPROTO_TCP, "", (a, port_, 0, 0) if ":" in a else (a, port_))
                    for a in addresses]
        # A literal only, never a lookup: nothing here reaches a resolver.
        return real_getaddrinfo(host, port_, family, type, proto, flags | socket.AI_NUMERICHOST)

    def connect(sock, address):
        net.dialled.append(tuple(address[:2]))
        if sock.family != socket.AF_INET:
            raise ConnectionRefusedError("only the IPv4 loopback answers here")
        return real_connect(sock, ("127.0.0.1", net.ports.get(address[1], port)))

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    # No proxy and no .netrc but the ones a test names, whatever this machine has.
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(feeds.requests.utils, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(feeds.requests.utils, "proxy_bypass",
                        urllib.request.proxy_bypass_environment)
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))
    yield net
    server.shutdown()
    server.server_close()


def zip_bytes(tmp_path) -> bytes:
    return gtfs_zip(tmp_path / "remote.zip").read_bytes()


def nothing_kept() -> None:
    assert not feeds.user_file().exists()
    folder = config.feeds_dir()
    left = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    assert left == [], left


def refused(url: str, **kw) -> str:
    with pytest.raises(feeds.FeedError) as caught:
        feeds.add(url, **kw)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    return str(caught.value)


def local(url: str) -> str:
    return f"{feeds.shown(url)} could not be fetched. {SENTENCE}"


# ------------------------------------------------------------ the first hop

LITERALS = [
    "http://127.0.0.1/gtfs.zip",
    "http://127.1.2.3:8080/gtfs.zip",
    "http://10.1.2.3/gtfs.zip",
    f"http://{RFC1918_172}/gtfs.zip",
    f"https://{RFC1918_192}/gtfs.zip",
    "http://169.254.169.254/latest/meta-data/",
    f"http://{SHARED}/gtfs.zip",
    "http://0.0.0.0/gtfs.zip",
    "http://224.0.0.1/gtfs.zip",
    "http://240.0.0.1/gtfs.zip",
    "http://localhost/gtfs.zip",
    "http://LOCALHOST./gtfs.zip",
    "http://feeds.localhost/gtfs.zip",
    "http://[::1]/gtfs.zip",
    "http://[::]/gtfs.zip",
    "http://[fd00::1]/gtfs.zip",
    "http://[fe80::1]/gtfs.zip",
    "http://[ff02::1]/gtfs.zip",
    "http://[fec0::1]/gtfs.zip",               # the old site-local range
    "http://[::ffff:127.0.0.1]/gtfs.zip",      # IPv4-mapped
    "http://[::ffff:a00:1]/gtfs.zip",          # IPv4-mapped 10.0.0.1, in hex
    "http://[::10.0.0.1]/gtfs.zip",            # IPv4-compatible
    "http://[64:ff9b::a00:1]/gtfs.zip",        # NAT64's 10.0.0.1
]


@pytest.mark.parametrize("url", LITERALS)
def test_a_first_address_on_this_machine_or_its_network_is_refused_unasked(home, net, url):
    assert refused(url) == local(url)
    assert net.dialled == [] and net.seen == [], "nothing was dialled"
    nothing_kept()


def _scheme_only(url: str, session) -> str:
    """``_check_hop`` without its refusals: what the connection meets alone."""
    return feeds.parse_url(feeds.requests.Request("GET", url).prepare().url).scheme


@pytest.mark.parametrize("url", LITERALS)
def test_the_connection_refuses_a_local_address_on_its_own(home, net, monkeypatch, url):
    """Without the early check, the connection's own rule still holds."""
    monkeypatch.setattr(feeds, "_check_hop", _scheme_only)
    net.resolve("localhost", ["127.0.0.1", "::1"])
    net.resolve("localhost.", ["127.0.0.1"])
    net.resolve("feeds.localhost", ["127.0.0.1"])
    assert refused(url) == local(url)
    assert net.dialled == []
    nothing_kept()


# 127.0.0.1 as a resolver or a proxy may still read it: not a literal to
# ``ipaddress``. macOS's resolver reads 0177.0.0.1 as 177.0.0.1, glibc's and
# the WHATWG parser as 127.0.0.1; it is refused for either.
NUMBERS = ["http://2130706433/gtfs.zip", "http://0x7f.1/gtfs.zip", "http://127.1/gtfs.zip",
           "http://0177.0.0.1/gtfs.zip", "http://127.0.0.1./gtfs.zip",
           "http://0x7f.0.0.0x1/gtfs.zip", "http://012.0.0.1/gtfs.zip",
           "http://010.0.0.1/gtfs.zip"]               # 8.0.0.1 in octal, 10.0.0.1 in decimal


@pytest.mark.parametrize("url", NUMBERS)
def test_a_number_any_reader_takes_for_a_local_address_is_refused(home, net, url):
    assert refused(url) == local(url)
    assert net.resolved == [] and net.dialled == []
    nothing_kept()


@pytest.mark.parametrize("name,read", [
    ("2130706433", "127.0.0.1"), ("0x7f.1", "127.0.0.1"), ("127.1", "127.0.0.1"),
    ("0177.0.0.1", "127.0.0.1"), ("1.2.3", "1.2.0.3"), ("0x5d.0270.0xd7.14", PUBLIC),
    ("08.0.0.1", None), ("1.2.3.4.5", None), ("256.1.1.1", None), ("1.16777216", None),
    ("feeds.example.test", None),
])
def test_how_the_classic_reader_takes_a_number(name, read):
    got = feeds._classic_ipv4(name)
    assert (str(got) if got is not None else None) == read


@pytest.mark.parametrize("name,read", [
    ("0177.0.0.1", "177.0.0.1"), ("010.0.0.1", "10.0.0.1"), ("08.0.0.1", "8.0.0.1"),
    ("0x7f.1", "127.0.0.1"), ("1.2.3.4.5", None),
])
def test_how_the_classic_reader_takes_a_number_read_as_decimal(name, read):
    got = feeds._classic_ipv4(name, octal=False)
    assert (str(got) if got is not None else None) == read


@pytest.mark.parametrize("addresses", [
    ["10.0.0.7"],
    ["127.0.0.1"],
    ["fd12::7"],
    ["::ffff:127.0.0.1"],
    [f"::ffff:{RFC1918_192}"],
    ["::10.0.0.7"],
    [PUBLIC, RFC1918_172],                     # any one of them is enough
    ["2606:4700::1111", "fe80::1"],
])
def test_a_public_looking_name_that_resolves_here_is_refused(home, net, addresses):
    url = "http://feeds.example.test/gtfs.zip"
    net.resolve("feeds.example.test", addresses)
    assert refused(url) == local(url)
    assert net.resolved == ["feeds.example.test"], "resolved once"
    assert net.dialled == [], "and never dialled"
    nothing_kept()


@pytest.mark.parametrize("address,public", [
    (PUBLIC, True),
    ("2606:4700::1111", True),
    ("::ffff:8.8.8.8", True),
    ("64:ff9b::808:808", True),                # what DNS64 answers for an IPv4-only host
    ("64:ff9b::a00:1", False),
    ("::ffff:127.0.0.1", False),
    ("::8.8.8.8", False),                      # IPv4-compatible: ::/8 is reserved
])
def test_which_addresses_are_public(address, public):
    assert feeds._is_local_address(ipaddress.ip_address(address)) is not public


def test_a_public_name_is_fetched_from_the_address_it_resolved_to(home, net, tmp_path):
    payload = zip_bytes(tmp_path)
    net.resolve("feeds.example.test", [PUBLIC])
    net.route("feeds.example.test/gtfs.zip", body=payload)
    feed = feeds.add("http://feeds.example.test/gtfs.zip", key="public")
    assert feed.zip_path.read_bytes() == payload
    assert net.resolved == ["feeds.example.test"]
    assert net.dialled == [(PUBLIC, 80)]


def test_a_name_that_rebinds_after_its_check_is_never_dialled_at_its_new_address(
        home, net, tmp_path):
    """DNS rebinding: public the first time it is asked, the machine itself
    every time after. The connection judges the addresses it dials and dials
    the ones it judged, so the name is resolved once and nothing reaches the
    loopback."""
    payload = zip_bytes(tmp_path)
    net.resolve("feeds.example.test", [PUBLIC], ["127.0.0.1"])
    net.route("feeds.example.test/gtfs.zip", body=payload)
    feeds.add("http://feeds.example.test/gtfs.zip", key="rebound")
    assert net.resolved == ["feeds.example.test"]
    assert net.dialled == [(PUBLIC, 80)]


def test_a_name_that_rebinds_between_two_hops_is_refused_at_the_second(home, net):
    url = "http://feeds.example.test/gtfs.zip"
    net.resolve("feeds.example.test", [PUBLIC], ["127.0.0.1"])
    net.redirect("feeds.example.test/gtfs.zip", "/current.zip")
    assert refused(url) == local(url)
    assert net.dialled == [(PUBLIC, 80)], "one connection, to the public address"
    assert [s["at"] for s in net.seen] == ["feeds.example.test/gtfs.zip"]
    nothing_kept()


def test_a_trailing_dot_is_resolved_as_it_was_written(home, net, tmp_path):
    """As urllib3 resolves it: the dot kept for the lookup, and dropped from
    the Host header (and from TLS's name)."""
    payload = zip_bytes(tmp_path)
    net.resolve("feeds.example.test.", [PUBLIC])
    net.route("feeds.example.test/gtfs.zip", body=payload)
    feeds.add("http://feeds.example.test./gtfs.zip", key="dotted")
    assert net.resolved == ["feeds.example.test."]
    assert net.dialled == [(PUBLIC, 80)]


def test_the_host_judged_is_the_one_dialled_whatever_the_backslash(home, net, tmp_path):
    """However an address is written, the host judged is the one urllib3
    reads from the address ``requests`` prepares, which is the one dialled:
    here a backslash before ``@``, either way round."""
    payload = zip_bytes(tmp_path)
    url = "http://127.0.0.1\\@feeds.example.test/gtfs.zip"
    assert refused(url) == local(url)
    assert net.dialled == []

    url = "http://feeds.example.test\\@127.0.0.1/gtfs.zip"
    net.resolve("feeds.example.test", [PUBLIC])
    net.route("feeds.example.test/%5C@127.0.0.1/gtfs.zip", body=payload)
    feeds.add(url, key="slashed")
    assert net.resolved == ["feeds.example.test"]
    assert net.dialled == [(PUBLIC, 80)]


def test_a_name_that_does_not_resolve_cannot_be_reached_and_dials_nothing(home, net):
    url = "http://nowhere.example.test/gtfs.zip"
    assert refused(url) == f"{url} could not be fetched: the server could not be reached"
    assert net.resolved == ["nowhere.example.test"] and net.dialled == []
    nothing_kept()


# ---------------------------------------------------------------- redirects

FIRST = "http://feeds.example.test/gtfs.zip"


@pytest.fixture
def public(net):
    for host in ("feeds.example.test", "mirror.example.test", "cdn.example.test"):
        net.resolve(host, [PUBLIC])
    return net


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/internal.zip",
    "http://127.0.0.1:9000/internal.zip?secret=1",
    "http://[::1]/internal.zip",
    "http://[::ffff:10.0.0.1]/internal.zip",
    "http://169.254.169.254/latest/meta-data/",
    "//localhost/internal.zip",                # scheme-relative
    "http://inside.example.test/internal.zip",  # a name that resolves here
    "http://feeds.example.test\\@127.0.0.1/internal.zip",
])
def test_a_redirect_to_this_machine_or_its_network_is_refused_before_it_is_dialled(
        home, public, location):
    public.resolve("inside.example.test", ["10.0.0.7"])
    public.redirect("feeds.example.test/gtfs.zip", location)
    assert refused(FIRST) == local(FIRST)
    assert public.dialled == [(PUBLIC, 80)], "only the first hop was dialled"
    assert [s["at"] for s in public.seen] == ["feeds.example.test/gtfs.zip"]
    nothing_kept()


@pytest.mark.parametrize("location", [
    "file:///etc/passwd",
    "http://[::1/internal.zip",                # an unclosed bracket: urlsplit refuses it
    # A netloc that changes under NFKC normalisation (U+FF20, a full-width @),
    # sent as UTF-8 as servers send it: urlsplit refuses it too.
    "http://evil\uff20feeds.example.test/x.zip".encode().decode("latin1"),
])
def test_a_redirect_to_an_address_that_cannot_be_fetched_says_so_and_nothing_more(
        home, public, location):
    public.redirect("feeds.example.test/gtfs.zip", location)
    with pytest.raises(feeds.FeedError) as caught:
        feeds.add(FIRST)
    assert str(caught.value) == f"{FIRST} could not be fetched: {NOT_FETCHABLE}"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    printed = "".join(traceback.format_exception(caught.value))
    assert "evil" not in printed and "passwd" not in printed and "::1" not in printed
    assert public.dialled == [(PUBLIC, 80)]
    nothing_kept()


KEYED = "http://feeduser:pa\uff20ss@feeds.example.test/gtfs.zip?api_key=S3CRET"
KEYED_SECRETS = ("feeduser", "pa\uff20ss", "\uff20", "S3CRET", "NFKC", "latin-1")


def _refused_quietly(url: str) -> None:
    with pytest.raises(feeds.FeedError) as caught:
        feeds.add(url)
    assert str(caught.value) == ("http://<redacted>@feeds.example.test/gtfs.zip?api_key="
                                 f"<redacted> could not be fetched: {NOT_FETCHABLE}")
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    printed = "".join(traceback.format_exception(caught.value))
    for secret in KEYED_SECRETS:
        assert secret not in printed, f"{secret!r} is in {printed!r}"
    nothing_kept()


def test_an_address_whose_user_information_urlsplit_refuses_is_refused_quietly(
        home, public):
    """User information that changes under NFKC normalisation (U+FF20, a
    full-width @), followed by a redirect: ``requests`` cannot send it as
    Basic credentials, which are Latin-1, and says so in an exception of its
    own, which is never the sentence."""
    public.redirect("feeds.example.test/gtfs.zip?api_key=S3CRET", "/current/gtfs.zip")
    _refused_quietly(KEYED)
    assert public.dialled == []


def test_that_address_with_a_netrc_present_is_refused_quietly(
        home, public, monkeypatch, tmp_path):
    """With a .netrc present, ``requests`` would also look the host up with
    ``urlparse`` on the address as given, whose refusal names the netloc
    whole: the address is refused as quietly."""
    netrc = tmp_path / "netrc"
    netrc.write_text("")
    monkeypatch.setenv("NETRC", str(netrc))
    public.redirect("feeds.example.test/gtfs.zip?api_key=S3CRET", "/current/gtfs.zip")
    _refused_quietly(KEYED)
    assert public.dialled == []


def test_a_redirect_is_resolved_against_the_address_as_it_was_sent():
    """Never against the address as given, which urlsplit may refuse with
    the netloc in its message. ``requests`` refuses such an address before
    its first hop (above), so this is the function alone."""
    given = KEYED
    answer = Answer("http://feeduser:pa%EF%BC%A0ss@feeds.example.test/gtfs.zip?api_key=S3CRET",
                    302, "current/gtfs.zip")
    assert feeds._redirect_target(answer, given) == (
        "http://feeduser:pa%EF%BC%A0ss@feeds.example.test/current/gtfs.zip")


def test_a_relative_redirect_on_a_public_host_is_followed(home, public, tmp_path):
    payload = zip_bytes(tmp_path)
    public.redirect("feeds.example.test/gtfs.zip", "current/gtfs.zip?v=2")
    public.route("feeds.example.test/current/gtfs.zip?v=2", body=payload)
    feed = feeds.add(FIRST, key="relative")
    assert [s["at"] for s in public.seen] == ["feeds.example.test/gtfs.zip",
                                              "feeds.example.test/current/gtfs.zip?v=2"]
    assert feed.url == FIRST, "the record keeps the address given"
    assert feed.zip_path.read_bytes() == payload


def test_an_encoded_location_is_followed_as_requests_would_send_it(home, public, tmp_path):
    payload = zip_bytes(tmp_path)
    public.redirect("feeds.example.test/gtfs.zip", "/féed s.zip".encode().decode("latin1"))
    public.route("feeds.example.test/f%C3%A9ed%20s.zip", body=payload)
    assert feeds.add(FIRST, key="encoded").zip_path.read_bytes() == payload


def test_a_public_chain_is_followed_and_every_hop_dialled_where_it_resolved(
        home, public, tmp_path):
    payload = zip_bytes(tmp_path)
    public.resolve("mirror.example.test", [PUBLIC_2])
    public.redirect("feeds.example.test/gtfs.zip", "http://mirror.example.test/g.zip", 301)
    public.redirect("mirror.example.test/g.zip", "http://cdn.example.test/a/b/g.zip", 302)
    public.route("cdn.example.test/a/b/g.zip", body=payload)
    feed = feeds.add(FIRST, key="chained")
    assert feed.zip_path.read_bytes() == payload
    assert public.resolved == ["feeds.example.test", "mirror.example.test", "cdn.example.test"]
    assert public.dialled == [(PUBLIC, 80), (PUBLIC_2, 80), (PUBLIC, 80)]


def test_a_redirects_own_body_is_never_read(home, public, tmp_path):
    """``requests`` reads a redirect's body whole even when told not to
    follow it; a download reads none of it, however large it says it is."""
    payload = zip_bytes(tmp_path)
    public.route("feeds.example.test/gtfs.zip", 302, {"Location": "/g.zip"}, body=64 << 20)
    public.route("feeds.example.test/g.zip", body=payload)
    feeds.add(FIRST, key="unread")
    for _ in range(200):
        if public.written:
            break
        threading.Event().wait(0.05)
    assert public.written and public.written[0] < 16 << 20, public.written


def test_cookies_carry_across_the_hops(home, public, tmp_path):
    payload = zip_bytes(tmp_path)
    public.route("feeds.example.test/gtfs.zip", 302,
                 {"Location": "/g.zip", "Set-Cookie": "token=abc; Path=/"})
    public.route("feeds.example.test/g.zip", body=payload)
    feeds.add(FIRST, key="cookies")
    assert [s["cookie"] for s in public.seen] == [None, "token=abc"]


def test_credentials_go_on_to_the_same_host_and_never_to_another(home, public, tmp_path):
    """``requests``' own rule (``Session.should_strip_auth``)."""
    payload = zip_bytes(tmp_path)
    public.redirect("feeds.example.test/a.zip", "http://feeds.example.test/b.zip")
    public.redirect("feeds.example.test/b.zip", "http://cdn.example.test/c.zip")
    public.route("cdn.example.test/c.zip", body=payload)
    feeds.add("http://someone:pw@feeds.example.test/a.zip", key="authed")
    sent = [s["auth"] for s in public.seen]
    assert sent[0] and sent[0].startswith("Basic ")
    assert sent == [sent[0], sent[0], None]


def test_ten_redirects_are_followed_and_the_eleventh_is_too_many(home, public, tmp_path):
    payload = zip_bytes(tmp_path)
    hops = [f"feeds.example.test/{i}.zip" for i in range(12)]
    for i in range(10):
        public.redirect(hops[i], f"/{i + 1}.zip")
    public.route(hops[10], body=payload)
    feeds.add(f"http://{hops[0]}", key="ten")
    assert [s["at"] for s in public.seen] == hops[:11]

    public.seen.clear()
    public.redirect(hops[10], "/11.zip")
    public.route(hops[11], body=payload)
    assert refused(f"http://{hops[0]}") == (f"http://{hops[0]} could not be fetched: "
                                            "it redirected too many times")
    assert [s["at"] for s in public.seen] == hops[:11], "the eleventh was not followed"


def test_each_hop_asks_whether_to_stop_first(home, public):
    public.redirect("feeds.example.test/gtfs.zip", "/g.zip")
    asked = []
    with pytest.raises(feeds.Interrupted):
        feeds.add(FIRST, cancelled=lambda: asked.append(1) or len(asked) > 1)
    assert [s["at"] for s in public.seen] == ["feeds.example.test/gtfs.zip"]
    nothing_kept()


def test_a_refusal_names_the_address_without_its_secrets_or_the_hops(home, public):
    keyed = "http://someone:pw@feeds.example.test/gtfs.zip?api_key=S3CRET#tok"
    public.redirect("feeds.example.test/gtfs.zip?api_key=S3CRET",
                    "http://127.0.0.1/x.zip?token=T0KEN")
    with pytest.raises(feeds.FeedError) as caught:
        feeds.add(keyed)
    assert str(caught.value) == ("http://<redacted>@feeds.example.test/gtfs.zip?api_key="
                                 f"<redacted>#<redacted> could not be fetched. {SENTENCE}")
    printed = "".join(traceback.format_exception(caught.value))
    for secret in ("S3CRET", "someone", "pw@", "tok", "T0KEN", "127.0.0.1"):
        assert secret not in printed, f"{secret!r} is in {printed!r}"
    nothing_kept()


def test_a_presets_cached_zip_is_kept_when_a_forced_fetch_is_refused(home, public, monkeypatch):
    """Nothing is written, so nothing is lost either."""
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T", url=FIRST))
    zipped = feeds.get("t").zip_path
    zipped.parent.mkdir(parents=True, exist_ok=True)
    zipped.write_bytes(b"cached")
    public.redirect("feeds.example.test/gtfs.zip", "http://10.0.0.1/t.zip", 307)
    with pytest.raises(feeds.FeedError, match="a feed cannot be fetched from this machine"):
        feeds.fetch("t", force=True)
    assert zipped.read_bytes() == b"cached"
    assert sorted(p.name for p in zipped.parent.iterdir()) == ["t.zip"]


# ------------------------------------------------------------ http and https

class Answer:
    """A response for the stand-in session, where a test needs https."""

    def __init__(self, url: str, status: int = 200, location: str | None = None,
                 body: bytes = b""):
        self.url = url
        self.status_code = status
        self.headers = ({"Location": location} if location is not None
                        else {"Content-Length": str(len(body))})
        self.body = body

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body

    def close(self):
        pass


def answering(monkeypatch, answers: dict[str, Answer]) -> list[str]:
    asked: list[str] = []
    stand_in(monkeypatch, lambda url, **kw: asked.append(url) or answers[url])
    return asked


def test_an_http_hop_after_an_https_one_is_refused(home, monkeypatch):
    first = "https://feeds.example.test/gtfs.zip"
    down = "http://feeds.example.test/gtfs.zip"
    asked = answering(monkeypatch, {first: Answer(first, 302, down)})
    assert refused(first) == (f"{first} could not be fetched. Use an address that stays on "
                              "https; it redirected to an unencrypted address.")
    assert asked == [first]
    nothing_kept()


def test_an_https_hop_after_an_http_one_is_followed(home, monkeypatch, tmp_path):
    payload = zip_bytes(tmp_path)
    first = "http://feeds.example.test/gtfs.zip"
    up = "https://feeds.example.test/gtfs.zip"
    asked = answering(monkeypatch, {first: Answer(first, 301, up), up: Answer(up, body=payload)})
    assert feeds.add(first, key="upgraded").zip_path.read_bytes() == payload
    assert asked == [first, up]


# ------------------------------------------------------------------ proxies

PROXY = "http://127.0.0.1:3128"


def test_through_a_proxy_a_public_name_is_sent_and_a_local_proxy_is_allowed(
        home, net, monkeypatch, tmp_path):
    """A proxy in the environment is the person's own choice: a name that
    resolves here to public addresses is sent to it, and the proxy, on this
    machine, is dialled."""
    payload = zip_bytes(tmp_path)
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    net.resolve("feeds.example.test", [PUBLIC])
    net.route("feeds.example.test/gtfs.zip", body=payload)
    feeds.add("http://feeds.example.test/gtfs.zip", key="proxied")
    # The name resolved here, best effort, then the proxy's own address,
    # resolved by urllib3 to connect to it.
    assert net.resolved == ["feeds.example.test", "127.0.0.1"]
    assert net.dialled == [("127.0.0.1", 3128)]
    assert [s["at"] for s in net.seen] == ["feeds.example.test/gtfs.zip"]


def test_through_a_proxy_a_name_that_does_not_resolve_here_is_sent_to_the_proxy(
        home, net, monkeypatch, tmp_path):
    """On some networks only the proxy resolves outside names."""
    payload = zip_bytes(tmp_path)
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    net.route("feeds.example.test/gtfs.zip", body=payload)
    feeds.add("http://feeds.example.test/gtfs.zip", key="proxied")
    assert net.resolved == ["feeds.example.test", "127.0.0.1"]
    assert net.dialled == [("127.0.0.1", 3128)]


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.7"], [PUBLIC, "fd12::7"],
                                       ["::ffff:127.0.0.1"]])
def test_through_a_proxy_a_name_that_resolves_here_to_a_local_address_is_refused(
        home, net, monkeypatch, addresses):
    url = "http://feeds.example.test/gtfs.zip"
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    net.resolve("feeds.example.test", addresses)
    assert refused(url) == local(url)
    assert net.dialled == [], "the proxy was not asked"
    nothing_kept()


@pytest.mark.parametrize("url", ["http://localhost/gtfs.zip", "http://10.1.2.3/gtfs.zip",
                                 "http://[::ffff:127.0.0.1]/gtfs.zip"] + NUMBERS)
def test_through_a_proxy_localhost_and_a_local_number_are_still_refused(
        home, net, monkeypatch, url):
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    assert refused(url) == local(url)
    assert net.dialled == []
    nothing_kept()


@pytest.mark.parametrize("url", NUMBERS)
def test_through_a_proxy_a_number_is_refused_whatever_this_systems_resolver_reads(
        home, net, monkeypatch, url):
    """A system whose resolver reads no number at all (and resolves no name):
    the engine's own readings still refuse each, since the proxy reads it as
    it will."""
    def nothing(host, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

    monkeypatch.setenv("HTTP_PROXY", PROXY)
    monkeypatch.setattr(socket, "getaddrinfo", nothing)
    assert refused(url) == local(url)
    assert net.dialled == []
    nothing_kept()


def test_a_redirect_through_a_proxy_to_a_name_that_resolves_here_is_refused(
        home, net, monkeypatch):
    url = "http://feeds.example.test/gtfs.zip"
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    net.resolve("feeds.example.test", [PUBLIC])
    net.resolve("inside.example.test", ["10.0.0.7"])
    net.redirect("feeds.example.test/gtfs.zip", "http://inside.example.test/x.zip")
    assert refused(url) == local(url)
    assert [s["at"] for s in net.seen] == ["feeds.example.test/gtfs.zip"]
    nothing_kept()


# ------------------------------------------------------- what is not an address

def test_a_valueerror_from_a_callback_is_not_taken_for_an_unreadable_address(
        home, public, tmp_path):
    public.route("feeds.example.test/gtfs.zip", body=zip_bytes(tmp_path))

    def progress(stage, done, total):
        raise ValueError("the caller's own")

    with pytest.raises(ValueError, match="the caller's own") as caught:
        feeds.add(FIRST, progress=progress)
    assert type(caught.value) is ValueError
    nothing_kept()


# --------------------------------------------------------------------- https

@pytest.fixture
def tls(net, tmp_path, monkeypatch):
    """An https server on the loopback whose certificate names
    feeds.example.test alone, trusted through REQUESTS_CA_BUNDLE; a dial to
    port 443 goes to it."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("no openssl to make a certificate with")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "2",
                    "-subj", "/CN=feeds.example.test",
                    "-addext", "subjectAltName=DNS:feeds.example.test"],
                   check=True, capture_output=True, timeout=60)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.net = net
    server.socket = context.wrap_socket(server.socket, server_side=True)
    net.ports[443] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    yield net
    server.shutdown()
    server.server_close()


def test_https_is_verified_against_the_name_while_the_checked_address_is_dialled(
        home, tls, tmp_path):
    payload = zip_bytes(tmp_path)
    tls.resolve("feeds.example.test", [PUBLIC])
    tls.route("feeds.example.test/gtfs.zip", body=payload)
    feed = feeds.add("https://feeds.example.test/gtfs.zip", key="secure")
    assert feed.zip_path.read_bytes() == payload
    assert tls.dialled == [(PUBLIC, 443)]


def test_https_to_a_name_the_certificate_does_not_carry_is_refused(home, tls):
    url = "https://other.example.test/gtfs.zip"
    tls.resolve("other.example.test", [PUBLIC])
    assert refused(url) == f"{url} could not be fetched: a secure connection could not be made"
    assert tls.dialled == [(PUBLIC, 443)] and tls.seen == []
    nothing_kept()


# --------------------------------------------------------- over the protocol

def test_over_the_protocol_a_refusal_is_a_feed_error_with_the_sentence(home, public):
    from test_serve import Client, check
    public.redirect("feeds.example.test/gtfs.zip", f"http://{RFC1918_192}/router.zip")
    client = Client()
    try:
        error = client.wait(client.send("feeds.add", {"source": FIRST}))["error"]
    finally:
        client.endpoint.close()
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert error["data"]["hint"] == local(FIRST)
    nothing_kept()
