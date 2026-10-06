"""Postings endpoint: auth, limits and fan out, with the reader stubbed."""

from datetime import datetime, timedelta, timezone

from app.posting_reader import Posting
from tests.conftest import StubSupabase, make_client
from app.errors import AppError

URL = "/api/v1/ai/import/postings"


class FakeReader:
    def __init__(self):
        self.urls: list[str] = []

    async def read(self, url):
        self.urls.append(url)
        return Posting("ok", f"text of {url}") if "good" in url else Posting("blocked")


def client_with_reader(**kwargs):
    client = make_client(**kwargs)
    fake = FakeReader()
    client.app.state.posting_service.reader = fake
    return client, fake


def post(client, urls, token="token"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(URL, headers=headers, json={"urls": urls})


def test_returns_postings_in_order_deduped():
    client, fake = client_with_reader()
    with client:
        response = post(client, ["https://good/1", "https://bad/2", "https://good/1"])
    assert response.status_code == 200
    assert response.json() == {
        "postings": [
            {"url": "https://good/1", "status": "ok", "text": "text of https://good/1"},
            {"url": "https://bad/2", "status": "blocked", "text": None},
        ]
    }
    assert fake.urls.count("https://good/1") == 1


def test_requires_a_session():
    client, _ = client_with_reader()
    with client:
        assert post(client, ["https://good/1"], token=None).status_code == 401
    client, _ = client_with_reader(
        supabase=StubSupabase(fail=AppError(401, "invalid_session", "no"))
    )
    with client:
        assert post(client, ["https://good/1"]).status_code == 401


def test_body_limits():
    client, _ = client_with_reader()
    with client:
        assert post(client, []).status_code == 422
        assert post(client, [f"https://good/{n}" for n in range(11)]).status_code == 422
        assert post(client, ["https://good/" + "a" * 2100]).status_code == 422


def test_rate_limit_per_user_window():
    now = [datetime(2026, 10, 6, 12, tzinfo=timezone.utc)]
    client, _ = client_with_reader(clock=lambda: now[0])
    with client:
        for _ in range(3):
            assert (
                post(client, [f"https://good/{n}" for n in range(10)]).status_code
                == 200
            )
        limited = post(client, ["https://good/x"])
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) > 0
        now[0] += timedelta(minutes=11)
        assert post(client, ["https://good/x"]).status_code == 200
