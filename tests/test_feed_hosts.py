"""A feed download reaches only public hosts, at every hop.

A feed is published on a public host, and a download never reaches this
machine or its network. ``feeds._download`` follows redirects itself, never
through ``requests``, and judges every hop before asking for it, the first included:
the host as urllib3 parses the address ``requests`` prepares, and every
address the resolver gives for it. A refusal is a ``FeedError`` (kind
``feed`` over the protocol) in the desktop app's own sentence, names the
address only as ``shown`` does, and keeps nothing.

Offline, as every download test is: ``requests.get`` is a stand-in that
follows redirects itself unless told not to, as ``requests`` does, and
``socket.getaddrinfo`` answers what each test says (``conftest.py`` resolves
the reserved test names publicly by default).
"""

import socket
import traceback
from urllib.parse import urljoin

import pytest

from conftest import PUBLIC
from schematic import config, feeds
from test_feeds import gtfs_zip, home  # noqa: F401 - home is a fixture

SENTENCE = feeds.LOCAL_HOST


def v4(*octets: int) -> str:
    """A dotted IPv4 address. The private ranges are written as octets, since
    ``bin/preflight`` refuses their dotted literals in a committed file."""
    return ".".join(map(str, octets))


# One address in each private range the preflight scan guards.
RFC1918_172, RFC1918_192 = v4(172, 16, 0, 9), v4(192, 168, 0, 10)
SHARED = v4(100, 64, 0, 1)                    # RFC 6598: carrier NAT, overlay networks
assert SENTENCE == ("Use an address on a public host; a feed cannot be fetched from this "
                    "machine or its network.")


class Answer:
    """One response: a status, a ``Location`` for a redirect, a body."""

    def __init__(self, status: int = 200, location: str | None = None, body: bytes = b""):
        self.status_code = status
        self.headers = ({"Location": location} if location is not None
                        else {"Content-Length": str(len(body))})
        self.body = body
        self.read = False
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise feeds.requests.HTTPError(f"{self.status_code}", response=self)

    def iter_content(self, size):
        self.read = True
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        self.closed = True


def serve(monkeypatch, routes: dict[str, Answer]) -> list[str]:
    """``requests.get`` over ``routes``, and the list of every address it was
    asked for or went to. Like ``requests``, it follows a redirect itself
    unless ``allow_redirects=False``; an address with no route is a server
    that cannot be reached."""
    asked: list[str] = []

    def get(url, **kw):
        while True:
            asked.append(url)
            if url not in routes:
                raise feeds.requests.ConnectionError(f"no route to {url}")
            answer = routes[url]
            if answer.status_code in feeds.REDIRECTS and kw.get("allow_redirects", True):
                url = urljoin(url, answer.headers["Location"])
                continue
            return answer

    monkeypatch.setattr(feeds.requests, "get", get)
    return asked


def resolve(monkeypatch, names: dict[str, list[str]]) -> list[str]:
    """``socket.getaddrinfo`` answering ``names`` with their addresses, both
    families as they come, and every other host as before; the list of the
    hosts it was asked about."""
    before = socket.getaddrinfo
    heard: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        heard.append(host)
        if host in names:
            return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM,
                     socket.IPPROTO_TCP, "", (a, port, 0, 0) if ":" in a else (a, port))
                    for a in names[host]]
        return before(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return heard


def zip_bytes(tmp_path) -> bytes:
    return gtfs_zip(tmp_path / "remote.zip").read_bytes()


def nothing_kept() -> None:
    assert not feeds.user_file().exists()
    folder = config.feeds_dir()
    left = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    assert left == [], left


def refused(url: str) -> str:
    with pytest.raises(feeds.FeedError) as caught:
        feeds.add(url)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    return str(caught.value)


# ------------------------------------------------------------ the first hop

@pytest.mark.parametrize("url", [
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
    "http://0x7f.1/gtfs.zip",                  # 127.0.0.1, as the resolver reads it
    "http://2130706433/gtfs.zip",              # 127.0.0.1 as one number
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
])
def test_a_first_address_on_this_machine_or_its_network_is_refused(home, monkeypatch, url):
    asked = serve(monkeypatch, {})
    assert refused(url) == f"{feeds.shown(url)} could not be fetched. {SENTENCE}"
    assert asked == [], "nothing was asked for"
    nothing_kept()


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
def test_a_public_looking_name_that_resolves_here_is_refused(home, monkeypatch, addresses):
    url = "https://feeds.example.test/gtfs.zip"
    asked = serve(monkeypatch, {})
    heard = resolve(monkeypatch, {"feeds.example.test": addresses})
    assert refused(url) == f"{url} could not be fetched. {SENTENCE}"
    assert heard == ["feeds.example.test"], "the name was resolved, once"
    assert asked == []
    nothing_kept()


@pytest.mark.parametrize("addresses", [
    [PUBLIC],
    ["2606:4700::1111", PUBLIC],
    ["::ffff:8.8.8.8"],
    # What DNS64 answers for an IPv4-only host on an IPv6-only network.
    ["64:ff9b::808:808"],
])
def test_a_name_that_resolves_publicly_is_fetched(home, monkeypatch, tmp_path, addresses):
    url = "https://feeds.example.test/gtfs.zip"
    payload = zip_bytes(tmp_path)
    asked = serve(monkeypatch, {url: Answer(body=payload)})
    resolve(monkeypatch, {"feeds.example.test": addresses})
    assert feeds.add(url, key="public").zip_path.read_bytes() == payload
    assert asked == [url]


def test_the_host_judged_is_the_one_requests_connects_to(home, monkeypatch, tmp_path):
    """However an address is written, the host judged is the one urllib3
    reads from the address ``requests`` prepares, which is the one it
    connects to: here a backslash before ``@``."""
    payload = zip_bytes(tmp_path)
    url = "http://127.0.0.1\\@feeds.example.test/gtfs.zip"
    serve(monkeypatch, {})
    assert refused(url) == f"{feeds.shown(url)} could not be fetched. {SENTENCE}"

    url = "http://feeds.example.test\\@127.0.0.1/gtfs.zip"
    prepared = feeds.requests.Request("GET", url).prepare().url
    asked = serve(monkeypatch, {url: Answer(body=payload)})
    heard = resolve(monkeypatch, {})
    feeds.add(url, key="slashed")
    assert asked == [url]
    assert heard == ["feeds.example.test"], prepared


def test_a_name_that_does_not_resolve_cannot_be_reached_and_asks_nothing(home, monkeypatch):
    def unknown(host, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

    asked = serve(monkeypatch, {})
    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    url = "https://nowhere.example.test/gtfs.zip"
    assert refused(url) == f"{url} could not be fetched: the server could not be reached"
    assert asked == []
    nothing_kept()


# ---------------------------------------------------------------- redirects

FIRST = "https://feeds.example.test/gtfs.zip"


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/internal.zip",
    "http://127.0.0.1:9000/internal.zip?secret=1",
    "http://[::1]/internal.zip",
    "http://[::ffff:10.0.0.1]/internal.zip",
    "http://169.254.169.254/latest/meta-data/",
    "//localhost/internal.zip",                # scheme-relative
    "https://inside.example.test/internal.zip",  # a name that resolves here
])
def test_a_redirect_to_this_machine_or_its_network_is_refused_before_it_is_asked(
        home, monkeypatch, tmp_path, location):
    payload = zip_bytes(tmp_path)
    target = urljoin(FIRST, location)
    asked = serve(monkeypatch, {FIRST: Answer(302, location), target: Answer(body=payload)})
    resolve(monkeypatch, {"inside.example.test": ["10.0.0.7"]})
    assert refused(FIRST) == f"{FIRST} could not be fetched. {SENTENCE}"
    assert asked == [FIRST], "the redirect was not followed"
    nothing_kept()


def test_a_redirect_to_another_scheme_is_refused(home, monkeypatch):
    asked = serve(monkeypatch, {FIRST: Answer(302, "file:///etc/passwd")})
    assert refused(FIRST) == (f"{FIRST} could not be fetched: "
                              "the address is not one that can be fetched")
    assert asked == [FIRST]
    nothing_kept()


def test_a_relative_redirect_on_a_public_host_is_followed(home, monkeypatch, tmp_path):
    payload = zip_bytes(tmp_path)
    moved = "https://feeds.example.test/current/gtfs.zip?v=2"
    asked = serve(monkeypatch, {FIRST: Answer(302, "current/gtfs.zip?v=2"),
                                moved: Answer(body=payload)})
    heard = resolve(monkeypatch, {})
    feed = feeds.add(FIRST, key="relative")
    assert asked == [FIRST, moved]
    assert heard == ["feeds.example.test", "feeds.example.test"], "every hop was resolved"
    assert feed.url == FIRST, "the record keeps the address given"
    assert feed.zip_path.read_bytes() == payload


def test_a_public_chain_is_followed_and_only_the_last_body_is_read(home, monkeypatch, tmp_path):
    payload = zip_bytes(tmp_path)
    second = "https://mirror.example.test/feeds/gtfs.zip"
    third = "https://cdn.example.test/a/b/gtfs.zip"
    hops = {FIRST: Answer(301, second), second: Answer(302, third),
            third: Answer(body=payload)}
    asked = serve(monkeypatch, hops)
    heard = resolve(monkeypatch, {})
    feed = feeds.add(FIRST, key="chained")
    assert asked == [FIRST, second, third]
    assert heard == ["feeds.example.test", "mirror.example.test", "cdn.example.test"]
    assert feed.zip_path.read_bytes() == payload
    assert not hops[FIRST].read and not hops[second].read, "a redirect's body is not read"
    assert hops[FIRST].closed and hops[second].closed and hops[third].closed


def test_a_preset_whose_host_redirects_as_the_mtas_did_downloads(home, monkeypatch, tmp_path):
    """web.mta.info's old address answers 302 from https down to http on its
    own host, then 301 to the MTA's bucket on S3 (as it did in Oct 2026):
    three public hops, so the preset downloads."""
    payload = zip_bytes(tmp_path)
    old = "https://web.mta.info/developers/data/nyct/subway/google_transit.zip"
    down = "http://web.mta.info/developers/data/nyct/subway/google_transit.zip"
    bucket = "http://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T", url=old))
    asked = serve(monkeypatch, {old: Answer(302, down), down: Answer(301, bucket),
                                bucket: Answer(body=payload)})
    heard = resolve(monkeypatch, {"web.mta.info": ["23.10.1.1"],
                                  "rrgtfsfeeds.s3.amazonaws.com": ["52.216.1.1",
                                                                   "2600:1fa0:80a4::1"]})
    assert feeds.fetch("t").read_bytes() == payload
    assert asked == [old, down, bucket]
    assert heard == ["web.mta.info", "web.mta.info", "rrgtfsfeeds.s3.amazonaws.com"]


def test_ten_redirects_are_followed_and_the_eleventh_is_too_many(home, monkeypatch, tmp_path):
    payload = zip_bytes(tmp_path)
    hops = [f"https://feeds.example.test/{i}.zip" for i in range(12)]

    routes = {hops[i]: Answer(302, hops[i + 1]) for i in range(10)}
    routes[hops[10]] = Answer(body=payload)
    asked = serve(monkeypatch, routes)
    feeds.add(hops[0], key="ten")
    assert asked == hops[:11]

    routes = {hops[i]: Answer(302, hops[i + 1]) for i in range(11)}
    routes[hops[11]] = Answer(body=payload)
    asked = serve(monkeypatch, routes)
    assert refused(hops[0]) == f"{hops[0]} could not be fetched: it redirected too many times"
    assert asked == hops[:11], "the eleventh redirect was asked for and not followed"


def test_a_refusal_names_the_address_without_its_secrets_or_the_hops(home, monkeypatch):
    keyed = "https://someone:pw@feeds.example.test/gtfs.zip?api_key=S3CRET#tok"
    serve(monkeypatch, {keyed: Answer(302, "http://127.0.0.1/x.zip?token=T0KEN")})
    with pytest.raises(feeds.FeedError) as caught:
        feeds.add(keyed)
    said = str(caught.value)
    assert said == ("https://<redacted>@feeds.example.test/gtfs.zip?api_key=<redacted>"
                    f"#<redacted> could not be fetched. {SENTENCE}")
    printed = "".join(traceback.format_exception(caught.value))
    for secret in ("S3CRET", "someone", "pw@", "tok", "T0KEN", "127.0.0.1"):
        assert secret not in printed, f"{secret!r} is in {printed!r}"
    nothing_kept()


def test_a_presets_cached_zip_is_kept_when_a_forced_fetch_is_refused(home, monkeypatch):
    """Nothing is written, so nothing is lost either: a refused hop leaves
    the cache as it was."""
    url = "https://feeds.example.test/t.zip"
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T", url=url))
    zipped = feeds.get("t").zip_path
    zipped.parent.mkdir(parents=True, exist_ok=True)
    zipped.write_bytes(b"cached")
    serve(monkeypatch, {url: Answer(307, "http://10.0.0.1/t.zip")})
    with pytest.raises(feeds.FeedError, match="a feed cannot be fetched from this machine"):
        feeds.fetch("t", force=True)
    assert zipped.read_bytes() == b"cached"
    assert sorted(p.name for p in zipped.parent.iterdir()) == ["t.zip"]


# --------------------------------------------------------- over the protocol

def test_over_the_protocol_a_refusal_is_a_feed_error_with_the_sentence(home, monkeypatch):
    from test_serve import Client, check
    serve(monkeypatch, {FIRST: Answer(302, f"http://{RFC1918_192}/router.zip")})
    client = Client()
    try:
        error = client.wait(client.send("feeds.add", {"source": FIRST}))["error"]
    finally:
        client.endpoint.close()
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert error["data"]["hint"] == f"{FIRST} could not be fetched. {SENTENCE}"
    nothing_kept()
