"""AnySearch finance.news: schema, auth, quota, unknown dates, and secret redaction."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import requests

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.news_support import clear_news_cache
from tradingagents.dataflows.vendors.anysearch import news as anysearch

SECRET = "as-sk-test-DO_NOT_LEAK_9f3c"
PASSWORD = "anysearch-password-DO_NOT_LEAK"


class _Resp:
    def __init__(self, status, payload=None, headers=None, raw=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self._raw = raw
        self.content = raw if raw is not None else json.dumps(payload or {}).encode()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    clear_news_cache()
    anysearch.clear_quota_backoff()
    monkeypatch.setenv("ANYSEARCH_API_KEY", SECRET)
    set_config({
        "data_cache_dir": str(tmp_path),
        "news_cache_ttl_seconds": 900,
        "news_http_max_retries": 1,
        "anysearch_macro_query_limit": 2,
        "anysearch_quota_backoff_seconds": 60,
    })
    monkeypatch.setattr("tradingagents.dataflows.news_support._sleep", lambda seconds: None)
    yield
    clear_news_cache()
    anysearch.clear_quota_backoff()


def _http(monkeypatch, responses):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        made = responses.pop(0)
        if isinstance(made, Exception):
            raise made
        return made

    monkeypatch.setattr("tradingagents.dataflows.news_support.requests.request", request)
    return calls


def _window():
    today = datetime.now(UTC).date()
    return (today - timedelta(days=3)).isoformat(), today.isoformat()


def _ok(results, request_id="req-ok"):
    return _Resp(
        200,
        {"code": 0, "message": "success", "request_id": request_id,
         "data": {"results": results, "metadata": {"total_results": len(results)}}},
        headers={"X-Request-ID": request_id},
    )


@pytest.mark.unit
def test_stock_schema_uses_headline_and_snippet_not_content(monkeypatch):
    start, end = _window()
    calls = _http(monkeypatch, [_ok([{
        "title": "AMD raises outlook",
        "url": "https://www.example.com/amd",
        "snippet": "The company raised its outlook.",
        "content": "FULLTEXT_SHOULD_NOT_APPEAR published on 1999-01-01",
    }])])

    out = anysearch.get_news("amd", start, end)

    assert "AMD raises outlook" in out
    assert "Snippet: The company raised its outlook." in out
    assert "Link: https://www.example.com/amd" in out
    assert "Publisher: example.com" in out
    assert "FULLTEXT_SHOULD_NOT_APPEAR" not in out
    assert "1999-01-01" not in out
    assert "Published: unknown" in out
    assert "not historical validated evidence" in out
    assert "not the full article" in out
    assert "Attribution: AnySearch finance.news type=stock symbol=AMD" in out
    assert "Undated items: 1" in out
    assert SECRET not in out
    payload = calls[0][2]["json"]
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://api.anysearch.com/v1/search"
    assert payload["tag"] == "finance.news"
    assert payload["query"] == "AMD stock news"
    assert payload["params"] == {"type": "stock", "symbol": "AMD", "period": "4d"}
    assert payload["language"] == "en"
    assert payload["zone"] == "intl"
    assert payload["max_results"] == 10
    assert calls[0][2]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert "content" not in payload


@pytest.mark.unit
def test_macro_uses_type_general_and_topic_queries(monkeypatch):
    _start, end = _window()
    calls = _http(monkeypatch, [
        _ok([{"title": "Fed watch", "url": "https://news.example/fed", "snippet": "rates"}]),
        _ok([{"title": "Earnings week", "url": "https://news.example/earn", "snippet": "gdp"}]),
        _ok([{"title": "SHOULD NOT REQUEST", "url": "https://news.example/no", "snippet": "x"}]),
    ])

    out = anysearch.get_global_news(end, look_back_days=3, limit=10)

    assert len(calls) == 2
    assert calls[0][2]["json"]["params"] == {"type": "general", "period": "4d"}
    assert "symbol" not in calls[0][2]["json"]["params"]
    assert "Federal Reserve" in calls[0][2]["json"]["query"]
    assert "S&P 500" in calls[1][2]["json"]["query"]
    assert "Fed watch" in out and "Earnings week" in out
    assert "SHOULD NOT REQUEST" not in out
    assert "type=general" in out


@pytest.mark.unit
def test_known_published_at_is_kept_and_a_future_one_is_dropped(monkeypatch):
    start, end = _window()
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat() + "T15:04:00Z"
    _http(monkeypatch, [_ok([
        {"title": "YESTERDAY", "url": "https://news.example/y", "snippet": "in range",
         "published_at": yesterday},
        {"title": "FUTURE", "url": "https://news.example/f", "snippet": "ahead",
         "published_at": "2099-01-01T00:00:00Z"},
        {"title": "GARBAGE DATE", "url": "https://news.example/g", "snippet": "no real time",
         "published_at": "not-a-date"},
    ])])

    out = anysearch.get_news("AMD", start, end)

    assert "YESTERDAY" in out
    assert "publication time known" in out
    assert yesterday[:10] in out
    assert "FUTURE" not in out
    assert "GARBAGE DATE" in out
    assert "Published: unknown" in out


@pytest.mark.unit
def test_historical_window_does_not_call_the_api(monkeypatch):
    def request(*args, **kwargs):
        raise AssertionError("historical AnySearch must not be requested")

    monkeypatch.setattr("tradingagents.dataflows.news_support.requests.request", request)
    with pytest.raises(VendorUnavailableError, match="not historical validated evidence"):
        anysearch.get_news("AMD", "2024-01-01", "2024-01-08")
    with pytest.raises(VendorUnavailableError, match="not historical validated evidence"):
        anysearch.get_global_news("2024-01-08", 7, 5)


@pytest.mark.unit
def test_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    start, end = _window()

    def request(*args, **kwargs):
        raise AssertionError("missing key must not call AnySearch")

    monkeypatch.setattr("tradingagents.dataflows.news_support.requests.request", request)
    with pytest.raises(VendorNotConfiguredError, match="ANYSEARCH_API_KEY"):
        anysearch.get_news("AMD", start, end)


@pytest.mark.unit
def test_auth_failure_hides_the_body(monkeypatch):
    start, end = _window()
    _http(monkeypatch, [_Resp(
        401,
        raw=json.dumps({"message": f"bad key {SECRET}"}).encode(),
        headers={"X-Request-ID": "req-auth"},
    )])
    with pytest.raises(VendorNotConfiguredError) as caught:
        anysearch.get_news("AMD", start, end)
    assert SECRET not in str(caught.value)
    assert "bad key" not in str(caught.value)
    assert "HTTP 401" in str(caught.value)
    assert "req-auth" in str(caught.value)


@pytest.mark.unit
def test_server_error_is_unavailable(monkeypatch):
    start, end = _window()
    calls = _http(monkeypatch, [
        _Resp(502, headers={"X-Request-ID": "req-bad"}),
        _Resp(502, headers={"X-Request-ID": "req-bad-2"}),
    ])
    with pytest.raises(VendorUnavailableError, match="HTTP 502"):
        anysearch.get_news("AMD", start, end)
    assert len(calls) == 2  # one bounded retry


@pytest.mark.unit
def test_quota_body_is_not_logged_and_further_calls_back_off(monkeypatch, caplog):
    start, end = _window()
    calls = _http(monkeypatch, [_Resp(
        402,
        raw=json.dumps({
            "message": f"username=somebody password={PASSWORD} api_key={SECRET}",
        }).encode(),
        headers={"X-Request-ID": "req-quota"},
    )])
    with caplog.at_level("WARNING"):
        with pytest.raises(VendorUnavailableError) as caught:
            anysearch.get_news("AMD", start, end)
        with pytest.raises(VendorUnavailableError, match="quota backoff"):
            anysearch.get_news("AMD", start, end)
    assert len(calls) == 1
    assert PASSWORD not in str(caught.value)
    assert SECRET not in str(caught.value)
    assert PASSWORD not in caplog.text
    assert SECRET not in caplog.text
    assert "req-quota" in str(caught.value)


@pytest.mark.unit
def test_rate_limit_retries_once_then_backs_off_without_the_body(monkeypatch, caplog):
    start, end = _window()
    calls = _http(monkeypatch, [
        _Resp(429, raw=f"slow down {SECRET}".encode(), headers={"Retry-After": "0", "X-Request-ID": "req-rl"}),
        _Resp(429, raw=f"still {PASSWORD}".encode(), headers={"Retry-After": "30", "X-Request-ID": "req-rl2"}),
    ])
    with caplog.at_level("WARNING"), pytest.raises(VendorUnavailableError, match="HTTP 429") as caught:
        anysearch.get_news("AMD", start, end)
    assert len(calls) == 2
    assert SECRET not in str(caught.value) and PASSWORD not in str(caught.value)
    assert SECRET not in caplog.text and PASSWORD not in caplog.text
    calls.clear()
    with pytest.raises(VendorUnavailableError, match="quota backoff"):
        anysearch.get_news("NVDA", start, end)
    assert calls == []


@pytest.mark.unit
def test_transport_error_redacts_the_key(monkeypatch, caplog):
    start, end = _window()
    _http(monkeypatch, [
        requests.ConnectionError(f"timed out Authorization: Bearer {SECRET}"),
        requests.ConnectionError(f"timed out Authorization: Bearer {SECRET}"),
    ])
    with caplog.at_level("WARNING"), pytest.raises(VendorUnavailableError) as caught:
        anysearch.get_news("AMD", start, end)
    assert SECRET not in str(caught.value)
    assert "Bearer ***" in str(caught.value)
    assert SECRET not in caplog.text


@pytest.mark.unit
def test_nonzero_code_does_not_surface_the_message(monkeypatch):
    start, end = _window()
    _http(monkeypatch, [_Resp(
        200,
        {"code": -1, "message": f"leaked {SECRET}", "request_id": "req-code"},
        headers={"X-Request-ID": "req-code"},
    )])
    with pytest.raises(VendorUnavailableError) as caught:
        anysearch.get_news("AMD", start, end)
    assert SECRET not in str(caught.value)
    assert "leaked" not in str(caught.value)
    assert "code -1" in str(caught.value)


@pytest.mark.unit
def test_results_that_are_not_a_list_are_unavailable(monkeypatch):
    start, end = _window()
    _http(monkeypatch, [_Resp(
        200,
        {"code": 0, "message": "success", "data": {"results": {"title": "nope"}}},
        headers={"X-Request-ID": "req-shape"},
    )])
    with pytest.raises(VendorUnavailableError, match="no results array"):
        anysearch.get_news("AMD", start, end)


@pytest.mark.unit
def test_empty_success_is_unavailable_not_silence(monkeypatch):
    start, end = _window()
    _http(monkeypatch, [_ok([])])
    with pytest.raises(VendorUnavailableError, match="no usable"):
        anysearch.get_news("AMD", start, end)


@pytest.mark.unit
def test_success_is_cached_without_the_key(monkeypatch, tmp_path):
    start, end = _window()
    calls = _http(monkeypatch, [
        _ok([{"title": "Cached AMD", "url": "https://news.example/c", "snippet": "s"}]),
        _ok([{"title": "SHOULD NOT FETCH", "url": "https://news.example/z", "snippet": "z"}]),
    ])
    first = anysearch.get_news("AMD", start, end)
    second = anysearch.get_news("AMD", start, end)
    assert first == second
    assert "Cached AMD" in first
    assert len(calls) == 1
    stored = list(tmp_path.rglob("*.json"))
    assert stored
    blob = "\n".join(path.read_text(encoding="utf-8") for path in stored)
    assert SECRET not in blob
    assert "Cached AMD" in blob


@pytest.mark.unit
def test_unsafe_symbol_is_not_sent(monkeypatch):
    start, end = _window()

    def request(*args, **kwargs):
        raise AssertionError("bad symbol must not be sent")

    monkeypatch.setattr("tradingagents.dataflows.news_support.requests.request", request)
    with pytest.raises(VendorUnavailableError, match="not a usable AnySearch"):
        anysearch.get_news("AMD OR secret", start, end)
