"""Posting reader guards, with DNS and HTTP stubbed; nothing touches the network."""

import asyncio

import httpx
import pytest

from app.posting_reader import PostingReader, html_to_text

PUBLIC = "93.184.216.34"


def reader(handler, addresses=None):
    table = addresses or {"jobs.example.com": [PUBLIC]}

    async def resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return PostingReader(resolve=resolve, transport=httpx.MockTransport(handler))


async def streamed(body: bytes):
    # Bytes content counts as already read in httpx; a socket streams it.
    yield body


def page(body="<p>We use React and TypeScript</p>", **headers):
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8", **headers},
        content=streamed(body.encode()),
    )


def run(r, url):
    return asyncio.run(r.read(url))


def test_reads_visible_text_from_pinned_ip_with_original_host():
    seen = {}

    def handler(request):
        seen.update(host=request.url.host, header=request.headers["host"])
        seen.update(sni=request.extensions.get("sni_hostname"))
        seen.update(
            cookie=request.headers.get("cookie"), ref=request.headers.get("referer")
        )
        return page()

    result = run(reader(handler), "https://jobs.example.com/1?jk=2")
    assert result.status == "ok" and result.text == "We use React and TypeScript"
    assert seen == {
        "host": PUBLIC,
        "header": "jobs.example.com",
        "sni": "jobs.example.com",
        "cookie": None,
        "ref": None,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.example.com/1",
        "https://jobs.example.com:8443/1",
        "https://user:pw@jobs.example.com/1",
        "ftp://jobs.example.com/1",
        "not a url",
    ],
)
def test_bad_urls_are_blocked_without_a_request(url):
    def handler(request):
        raise AssertionError("no request expected")

    assert run(reader(handler), url).status == "blocked"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "224.0.0.1",
        "::ffff:127.0.0.1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::a00:1",
        "::127.0.0.1",
    ],
)
def test_private_addresses_are_blocked(address):
    def handler(request):
        raise AssertionError("no request expected")

    r = reader(handler, {"jobs.example.com": [address]})
    assert run(r, "https://jobs.example.com/").status == "blocked"


def test_host_with_any_private_address_is_blocked():
    def handler(request):
        raise AssertionError("no request expected")

    r = reader(handler, {"jobs.example.com": [PUBLIC, "10.0.0.1"]})
    assert run(r, "https://jobs.example.com/").status == "blocked"


def test_unknown_host_is_unreachable():
    assert (
        run(reader(lambda r: page()), "https://nope.example.com/").status
        == "unreachable"
    )


def test_redirect_is_followed_and_rechecked():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(
                301, headers={"location": "https://jobs.example.com/new"}
            )
        return page("<p>Kotlin</p>")

    assert run(reader(handler), "https://jobs.example.com/old").text == "Kotlin"


def test_redirect_to_private_address_is_blocked():
    def handler(request):
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest"}
        )

    assert run(reader(handler), "https://jobs.example.com/").status == "blocked"


def test_too_many_redirects_is_unreachable():
    def handler(request):
        return httpx.Response(
            302, headers={"location": "https://jobs.example.com/loop"}
        )

    assert run(reader(handler), "https://jobs.example.com/").status == "unreachable"


def test_non_html_is_rejected():
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=streamed(b"%PDF")
        )

    assert run(reader(handler), "https://jobs.example.com/").status == "not_html"


def test_body_over_cap_is_too_large():
    def handler(request):
        return page("<p>" + "a" * 1_100_000 + "</p>")

    assert run(reader(handler), "https://jobs.example.com/").status == "too_large"


def test_error_status_and_timeout_are_unreachable():
    assert (
        run(reader(lambda r: httpx.Response(404)), "https://jobs.example.com/").status
        == "unreachable"
    )

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert run(reader(slow), "https://jobs.example.com/").status == "unreachable"


def test_page_without_text_is_empty():
    assert (
        run(
            reader(lambda r: page("<script>app()</script>")),
            "https://jobs.example.com/",
        ).status
        == "empty"
    )


def test_html_to_text_drops_scripts_styles_and_caps():
    html = "<style>p{}</style><h1>Role</h1><script>x()</script><p>Go &amp; Rust</p>"
    assert html_to_text(html) == "Role\nGo & Rust"
    assert len(html_to_text("<p>" + "a " * 20_000 + "</p>")) == 20_000


def test_html_to_text_reads_job_posting_structured_data():
    html = (
        "<div id=root></div>"
        '<script type="application/ld+json">'
        '{"@context": "https://schema.org", "@type": "JobPosting",'
        ' "description": "<p>We use <b>React</b> &amp; Go</p>"}</script>'
        '<script type="application/ld+json">{"@type": "Organization", "description": "Skip me"}</script>'
        '<script type="application/ld+json">not json</script>'
        '<script type="application/ld+json">"just a string"</script>'
    )
    assert html_to_text(html) == "We use\nReact\n& Go"


def test_cookies_set_on_a_redirect_are_not_sent_on_the_next_hop():
    sent = []

    def handler(request):
        sent.append(request.headers.get("cookie"))
        if request.url.path == "/old":
            return httpx.Response(
                302,
                headers={
                    "location": "https://jobs.example.com/new",
                    "set-cookie": "sid=abc; Path=/",
                },
            )
        return page()

    assert run(reader(handler), "https://jobs.example.com/old").status == "ok"
    assert sent == [None, None]


def test_slow_drip_body_hits_the_overall_deadline(monkeypatch):
    monkeypatch.setattr("app.posting_reader.TIMEOUT_SECONDS", 0.2)

    async def drip():
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"<p>a</p>"

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=drip()
        )

    assert run(reader(handler), "https://jobs.example.com/").status == "unreachable"


def test_streamed_body_stops_reading_at_the_cap():
    pulled = []

    async def endless():
        while True:
            pulled.append(1)
            yield b"a" * 65_536

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=endless()
        )

    assert run(reader(handler), "https://jobs.example.com/").status == "too_large"
    assert len(pulled) <= 17


def test_gzip_bomb_is_too_large_without_inflating_it():
    import gzip

    bomb = gzip.compress(b"a" * 50_000_000)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "gzip"},
            content=streamed(bomb),
        )

    import tracemalloc

    tracemalloc.start()
    status = run(reader(handler), "https://jobs.example.com/").status
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert status == "too_large"
    assert peak < 10_000_000


def test_gzip_page_is_read():
    import gzip

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "gzip"},
            content=streamed(gzip.compress(b"<p>Kotlin</p>")),
        )

    assert run(reader(handler), "https://jobs.example.com/").text == "Kotlin"
