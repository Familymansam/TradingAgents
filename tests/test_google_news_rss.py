"""Google News RSS: bad XML, date filters, time zones, cache, and live supplement."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.news_support import NewsItem, clear_news_cache
from tradingagents.dataflows.vendors.google_news import news as gnews


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}


def _rfc(day, time="12:00:00", tz="GMT"):
    stamp = datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return stamp.strftime("%a, %d %b %Y %H:%M:%S ") + tz


def _rss(items):
    rows = []
    for item in items:
        pub = item.get("pub")
        pub_xml = f"<pubDate>{pub}</pubDate>" if pub is not None else ""
        rows.append(
            "<item>"
            f"<title>{item['title']}</title>"
            f"<link>{item['link']}</link>"
            f"{pub_xml}"
            f"<description>{item.get('description', '')}</description>"
            f"<source url=\"{item.get('source_url', '')}\">{item.get('source', '')}</source>"
            "</item>"
        )
    return "<?xml version='1.0'?><rss version='2.0'><channel>" + "".join(rows) + "</channel></rss>"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    clear_news_cache()
    set_config({
        "data_cache_dir": str(tmp_path),
        "news_anysearch_supplement": False,
        "news_cache_ttl_seconds": 900,
        "news_http_max_retries": 1,
    })
    monkeypatch.setattr("tradingagents.dataflows.news_support._sleep", lambda seconds: None)
    yield
    clear_news_cache()


def _route_http(monkeypatch, responses):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        body = responses.pop(0)
        if isinstance(body, Exception):
            raise body
        return body

    monkeypatch.setattr("tradingagents.dataflows.news_support.requests.request", request)
    return calls


@pytest.mark.unit
def test_bad_xml_is_unavailable(monkeypatch):
    calls = _route_http(monkeypatch, [_Resp(200, "<rss><channel><item><title>x</title>")])
    with pytest.raises(VendorUnavailableError, match="not valid XML"):
        gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert len(calls) == 1


@pytest.mark.unit
def test_dtd_entity_is_refused():
    with pytest.raises(VendorUnavailableError, match="DTD"):
        gnews.parse_feed("<!DOCTYPE rss [<!ENTITY x 'y'>]><rss/>")


@pytest.mark.unit
def test_filters_future_items_and_keeps_the_end_date(monkeypatch):
    xml = _rss([
        {"title": "INSIDE", "link": "https://news.google.com/rss/articles/inside",
         "pub": "Fri, 09 May 2025 23:59:59 GMT", "source": "CNBC",
         "source_url": "https://www.cnbc.com", "description": "A real snippet"},
        {"title": "MIDNIGHT AFTER", "link": "https://news.google.com/rss/articles/after",
         "pub": "Sat, 10 May 2025 00:00:00 GMT", "source": "CNBC"},
        {"title": "BEFORE", "link": "https://news.google.com/rss/articles/before",
         "pub": "Wed, 30 Apr 2025 12:00:00 GMT", "source": "CNBC"},
    ])
    _route_http(monkeypatch, [_Resp(200, xml)])

    out = gnews.get_news("AMD", "2025-05-01", "2025-05-09")

    assert "INSIDE" in out
    assert "MIDNIGHT AFTER" not in out
    assert "BEFORE" not in out
    assert "2025-05-09 23:59 UTC (publication time known)" in out
    assert "not the full article" in out
    assert "Attribution: Google News RSS" in out
    assert "Publisher: CNBC" in out
    assert "Publisher site: https://www.cnbc.com" in out
    assert "Snippet: A real snippet" in out
    assert "Link: https://news.google.com/rss/articles/inside" in out
    assert "Dated items: 1" in out
    assert "Undated items: 0" in out


@pytest.mark.unit
def test_positive_offset_is_converted_to_utc(monkeypatch):
    # 2025-05-10 01:00 +05:00 is 2025-05-09 20:00 UTC, inside a window ending the 9th.
    xml = _rss([{
        "title": "OFFSET",
        "link": "https://news.google.com/rss/articles/offset",
        "pub": "Sat, 10 May 2025 01:00:00 +0500",
        "source": "Reuters",
    }])
    _route_http(monkeypatch, [_Resp(200, xml)])

    inside = gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert "OFFSET" in inside
    assert "2025-05-09 20:00 UTC" in inside


@pytest.mark.unit
def test_offset_falls_outside_the_utc_day(monkeypatch):
    xml = _rss([{
        "title": "NEXT UTC DAY",
        "link": "https://news.google.com/rss/articles/next",
        "pub": "Sat, 10 May 2025 01:00:00 +0500",
        "source": "Reuters",
    }])
    _route_http(monkeypatch, [_Resp(200, xml)])
    out = gnews.get_news("AMD", "2025-05-10", "2025-05-10")
    # 01:00 at UTC+5 is still 2025-05-09 UTC, so it is outside this window.
    assert "NEXT UTC DAY" not in out
    assert "publication time known" not in out


@pytest.mark.unit
def test_undated_rss_item_is_dropped_for_a_past_window(monkeypatch):
    xml = _rss([
        {"title": "NO DATE", "link": "https://news.google.com/rss/articles/nodate", "source": "CNBC"},
        {"title": "DATED", "link": "https://news.google.com/rss/articles/dated",
         "pub": "Mon, 05 May 2025 12:00:00 GMT", "source": "CNBC"},
    ])
    _route_http(monkeypatch, [_Resp(200, xml)])
    out = gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert "DATED" in out
    assert "NO DATE" not in out
    assert "Undated items: 0" in out


@pytest.mark.unit
def test_undated_rss_item_is_labeled_when_the_window_reaches_today(monkeypatch):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    xml = _rss([
        {"title": "NO DATE LIVE", "link": "https://news.google.com/rss/articles/live", "source": "CNBC"},
    ])
    _route_http(monkeypatch, [_Resp(200, xml)])
    out = gnews.get_news("AMD", today, today)
    assert "NO DATE LIVE" in out
    assert "Published: unknown" in out
    assert "not historical validated evidence" in out
    assert "Undated items: 1" in out


@pytest.mark.unit
def test_items_after_the_window_are_not_an_absence_of_news(monkeypatch):
    xml = _rss([{
        "title": "LATER",
        "link": "https://news.google.com/rss/articles/later",
        "pub": "Sun, 01 Jun 2025 12:00:00 GMT",
        "source": "CNBC",
    }])
    _route_http(monkeypatch, [_Resp(200, xml)])
    out = gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert "LATER" not in out
    assert "unavailable" in out and "not an absence" in out


@pytest.mark.unit
def test_macro_query_is_topic_constrained_and_date_bounded(monkeypatch):
    seen = []

    def request(method, url, **kwargs):
        seen.append(kwargs.get("params") or {})
        xml = _rss([{
            "title": "FED HOLDS",
            "link": "https://news.google.com/rss/articles/fed",
            "pub": "Thu, 08 May 2025 15:00:00 GMT",
            "source": "Reuters",
            "source_url": "https://www.reuters.com",
        }])
        return _Resp(200, xml)

    monkeypatch.setattr(
        "tradingagents.dataflows.news_support.requests.request", request,
    )
    out = gnews.get_global_news("2025-05-09", look_back_days=7, limit=5)

    assert "FED HOLDS" in out
    assert seen
    query = seen[0]["q"]
    assert query.startswith('(Federal Reserve OR FOMC)')
    assert "after:2025-05-02" in query and "before:2025-05-10" in query
    assert not query.startswith("((")
    assert seen[0]["hl"] == "en-US" and seen[0]["ceid"] == "US:en"


@pytest.mark.unit
def test_http_error_retries_once_then_succeeds(monkeypatch):
    xml = _rss([{
        "title": "RECOVERED",
        "link": "https://news.google.com/rss/articles/ok",
        "pub": "Thu, 08 May 2025 15:00:00 GMT",
        "source": "CNBC",
    }])
    calls = _route_http(monkeypatch, [_Resp(503, "nope"), _Resp(200, xml)])
    out = gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert "RECOVERED" in out
    assert len(calls) == 2


@pytest.mark.unit
def test_http_error_that_outlasts_the_retry_is_unavailable(monkeypatch):
    _route_http(monkeypatch, [_Resp(503, "nope"), _Resp(429, "still")])
    with pytest.raises(VendorUnavailableError, match="HTTP 429"):
        gnews.get_news("AMD", "2025-05-01", "2025-05-09")


@pytest.mark.unit
def test_client_error_is_not_retried(monkeypatch):
    calls = _route_http(monkeypatch, [_Resp(400, "bad")])
    with pytest.raises(VendorUnavailableError, match="HTTP 400"):
        gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert len(calls) == 1


@pytest.mark.unit
def test_successful_feed_is_cached(monkeypatch):
    xml = _rss([{
        "title": "CACHED",
        "link": "https://news.google.com/rss/articles/cached",
        "pub": "Thu, 08 May 2025 15:00:00 GMT",
        "source": "CNBC",
    }])
    calls = _route_http(monkeypatch, [_Resp(200, xml), _Resp(200, "<not-used/>")])
    assert "CACHED" in gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert "CACHED" in gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert len(calls) == 1


@pytest.mark.unit
def test_cache_expires(monkeypatch):
    xml = _rss([{
        "title": "ONCE",
        "link": "https://news.google.com/rss/articles/once",
        "pub": "Thu, 08 May 2025 15:00:00 GMT",
        "source": "CNBC",
    }])
    calls = _route_http(monkeypatch, [_Resp(200, xml), _Resp(200, xml)])
    # Start from the real clock so the file entry's mtime and the mocked clock
    # agree; then step well past the TTL.
    clock = {"now": time.time()}
    monkeypatch.setattr("tradingagents.dataflows.news_support.time.time", lambda: clock["now"])
    set_config({"news_cache_ttl_seconds": 10})
    gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    clock["now"] += 30
    gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert len(calls) == 2


@pytest.mark.unit
def test_duplicate_links_collapse(monkeypatch):
    xml = _rss([
        {"title": "SAME", "link": "https://news.google.com/rss/articles/same",
         "pub": "Thu, 08 May 2025 15:00:00 GMT", "source": "CNBC"},
        {"title": "SAME again", "link": "https://news.google.com/rss/articles/same",
         "pub": "Thu, 08 May 2025 16:00:00 GMT", "source": "CNBC"},
    ])
    _route_http(monkeypatch, [_Resp(200, xml)])
    out = gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert out.count("### SAME") == 1


@pytest.mark.unit
def test_unsafe_ticker_is_not_sent(monkeypatch):
    def request(*args, **kwargs):
        raise AssertionError("unsafe ticker must not be requested")

    monkeypatch.setattr("tradingagents.dataflows.news_support.requests.request", request)
    with pytest.raises(VendorUnavailableError, match="not a usable news symbol"):
        gnews.get_news("AMD OR site:evil.example", "2025-05-01", "2025-05-09")


@pytest.mark.unit
def test_live_supplement_is_appended_and_deduped(monkeypatch):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    xml = _rss([{
        "title": "RSS ONLY",
        "link": "https://news.google.com/rss/articles/rss",
        "pub": _rfc(today),
        "source": "CNBC",
        "source_url": "https://www.cnbc.com",
    }])
    _route_http(monkeypatch, [_Resp(200, xml)])
    set_config({"news_anysearch_supplement": True})
    extra = [
        NewsItem("RSS ONLY", "https://example.com/dup", "example.com", "dup", None,
                 "AnySearch finance.news type=stock symbol=AMD"),
        NewsItem("ANY ONLY", "https://example.com/amd", "example.com", "snippet text", None,
                 "AnySearch finance.news type=stock symbol=AMD"),
    ]
    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.anysearch.news.search_ticker",
        lambda *args, **kwargs: extra,
    )

    out = gnews.get_news("AMD", today, today)

    assert "RSS ONLY" in out
    assert "ANY ONLY" in out
    assert out.count("### RSS ONLY") == 1
    assert "not historical validated evidence" in out
    assert "Snippet: snippet text" in out


@pytest.mark.unit
def test_supplement_failure_keeps_the_rss_report(monkeypatch):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    xml = _rss([{
        "title": "STILL HERE",
        "link": "https://news.google.com/rss/articles/still",
        "pub": _rfc(today),
        "source": "CNBC",
    }])
    _route_http(monkeypatch, [_Resp(200, xml)])
    set_config({"news_anysearch_supplement": True})

    def broken(*args, **kwargs):
        raise VendorUnavailableError("AnySearch HTTP 502; request_id=abc")

    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.anysearch.news.search_ticker", broken,
    )
    out = gnews.get_news("AMD", today, today)
    assert "STILL HERE" in out
    assert "request_id=abc" not in out


@pytest.mark.unit
def test_past_window_does_not_call_anysearch(monkeypatch):
    xml = _rss([{
        "title": "OLD",
        "link": "https://news.google.com/rss/articles/old",
        "pub": "Thu, 08 May 2025 15:00:00 GMT",
        "source": "CNBC",
    }])
    _route_http(monkeypatch, [_Resp(200, xml)])
    set_config({"news_anysearch_supplement": True})

    def forbidden(*args, **kwargs):
        raise AssertionError("AnySearch must not run for a historical window")

    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.anysearch.news.search_ticker", forbidden,
    )
    out = gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert "OLD" in out
    assert "Undated items: 0" in out


@pytest.mark.unit
def test_description_that_is_only_a_link_is_not_a_snippet(monkeypatch):
    xml = _rss([{
        "title": "HEADLINE",
        "link": "https://news.google.com/rss/articles/head",
        "pub": "Thu, 08 May 2025 15:00:00 GMT",
        "source": "CNBC",
        "description": '<a href="https://news.google.com/rss/articles/head">https://news.google.com/rss/articles/head</a>',
    }])
    _route_http(monkeypatch, [_Resp(200, xml)])
    out = gnews.get_news("AMD", "2025-05-01", "2025-05-09")
    assert "Snippet: unavailable" in out
    assert "Form: headline and snippet only" in out


@pytest.mark.integration
def test_live_google_news_rss_amd_and_macro(tmp_path):
    """Hits the public RSS feed. Skipped unless pytest is run with -m integration."""
    set_config({
        "data_cache_dir": str(tmp_path),
        "news_anysearch_supplement": False,
        "news_http_max_retries": 1,
    })
    clear_news_cache()
    today = datetime.now(UTC).date()
    start = (today - timedelta(days=7)).isoformat()
    ticker = gnews.get_news("AMD", start, today.isoformat())
    macro = gnews.get_global_news(today.isoformat(), look_back_days=7, limit=5)
    assert "Google News RSS" in ticker
    assert "publication time known" in ticker
    assert "Link: http" in ticker
    assert "not the full article" in ticker
    assert "Google News RSS" in macro
    assert "Dated items:" in macro
    assert "publication time known" in macro
