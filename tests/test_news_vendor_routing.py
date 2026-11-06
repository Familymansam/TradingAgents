"""Default news routing uses Google News RSS, then AnySearch, and not Yahoo or Alpha Vantage."""

from datetime import UTC, datetime

import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.news_support import clear_news_cache
from tradingagents.dataflows.vendors.anysearch import news as anysearch
from tradingagents.dataflows.vendors.google_news.news import get_news as get_google_news
from tradingagents.default_config import DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    clear_news_cache()
    anysearch.clear_quota_backoff()
    set_config({"data_cache_dir": str(tmp_path)})
    yield
    clear_news_cache()
    anysearch.clear_quota_backoff()


def _chain(monkeypatch, method, impls):
    monkeypatch.setitem(router.VENDOR_METHODS, method, {
        **router.VENDOR_METHODS[method],
        **impls,
    })


@pytest.mark.unit
def test_defaults_leave_prices_and_fundamentals_alone():
    assert DEFAULT_CONFIG["tool_vendors"]["get_news"] == "google_news,anysearch"
    assert DEFAULT_CONFIG["tool_vendors"]["get_global_news"] == "google_news,anysearch"
    assert DEFAULT_CONFIG["data_vendors"]["news_data"] == "yfinance"
    assert DEFAULT_CONFIG["data_vendors"]["fundamental_data"] == "sec_edgar,yfinance"
    assert DEFAULT_CONFIG["data_vendors"]["core_stock_apis"] == "yfinance"
    assert DEFAULT_CONFIG["data_vendors"]["technical_indicators"] == "yfinance"
    assert "google_news" not in router.VENDOR_METHODS["get_insider_transactions"]
    assert "anysearch" not in router.VENDOR_METHODS["get_insider_transactions"]
    assert "google_news" not in router.VENDOR_METHODS["get_stock_data"]
    assert "google_news" not in router.VENDOR_METHODS["get_fundamentals"]
    # Yahoo's own macro query list is untouched so a local Yahoo news patch
    # still sees the queries it was written against.
    assert DEFAULT_CONFIG["global_news_queries"][0] == "Federal Reserve interest rates inflation"
    assert "FOMC" in DEFAULT_CONFIG["news_macro_queries"][0]


@pytest.mark.unit
def test_rss_success_does_not_call_later_vendors(monkeypatch):
    called = []

    def rss(*args, **kwargs):
        called.append("google_news")
        return "RSS BODY"

    def later(name):
        def impl(*args, **kwargs):
            called.append(name)
            return name
        return impl

    _chain(monkeypatch, "get_news", {
        "google_news": rss,
        "anysearch": later("anysearch"),
        "yfinance": later("yfinance"),
        "alpha_vantage": later("alpha_vantage"),
    })
    assert router.route_to_vendor("get_news", "AMD", "2026-11-01", "2026-11-06") == "RSS BODY"
    assert called == ["google_news"]


@pytest.mark.unit
def test_rss_outage_falls_back_to_anysearch_not_yahoo(monkeypatch):
    called = []

    def rss(*args, **kwargs):
        called.append("google_news")
        raise VendorUnavailableError("Google News RSS HTTP 503")

    def search(*args, **kwargs):
        called.append("anysearch")
        return "ANY BODY"

    def yahoo(*args, **kwargs):
        called.append("yfinance")
        return "YAHOO"

    _chain(monkeypatch, "get_news", {
        "google_news": rss,
        "anysearch": search,
        "yfinance": yahoo,
        "alpha_vantage": yahoo,
    })
    assert router.route_to_vendor("get_news", "AMD", "2026-11-01", "2026-11-06") == "ANY BODY"
    assert called == ["google_news", "anysearch"]


@pytest.mark.unit
def test_both_news_vendors_down_is_unavailable_not_an_exception(monkeypatch):
    def down(*args, **kwargs):
        raise VendorUnavailableError("down")

    _chain(monkeypatch, "get_global_news", {
        "google_news": down,
        "anysearch": down,
    })
    out = router.route_to_vendor("get_global_news", "2026-11-06", 7, 5)
    assert out.startswith("DATA_UNAVAILABLE")
    assert "delisted" not in out


@pytest.mark.unit
def test_explicit_yahoo_chain_is_still_available(monkeypatch):
    set_config({"tool_vendors": {"get_news": "yfinance"}})

    def yahoo(*args, **kwargs):
        return "YAHOO NEWS"

    def rss(*args, **kwargs):
        raise AssertionError("RSS must not run when the chain is Yahoo only")

    _chain(monkeypatch, "get_news", {"yfinance": yahoo, "google_news": rss})
    assert router.route_to_vendor("get_news", "AMD", "2026-11-01", "2026-11-06") == "YAHOO NEWS"


@pytest.mark.unit
def test_router_cache_and_secret_stay_inside_the_adapter(monkeypatch):
    """A real RSS fetch is cached; a 402 fallback does not copy the key into the tool result."""
    secret = "as-sk-test-DO_NOT_LEAK_router"
    password = "password-DO_NOT_LEAK_router"
    monkeypatch.setenv("ANYSEARCH_API_KEY", secret)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    xml = (
        "<?xml version='1.0'?><rss version='2.0'><channel><item>"
        "<title>Router AMD</title>"
        "<link>https://news.google.com/rss/articles/router</link>"
        f"<pubDate>{datetime.now(UTC).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>"
        "<source url='https://www.cnbc.com'>CNBC</source>"
        "<description>snippet</description>"
        "</item></channel></rss>"
    )
    calls = {"rss": 0, "any": 0}

    def request(method, url, **kwargs):
        if method == "GET":
            calls["rss"] += 1
            class Resp:
                status_code = 200
                content = xml.encode()
                headers = {}
            return Resp()
        calls["any"] += 1
        class Denied:
            status_code = 402
            headers = {"X-Request-ID": "req-router"}
            content = f"password={password} api_key={secret}".encode()

            def json(self):
                raise AssertionError("402 body must not be parsed")
        return Denied()

    monkeypatch.setattr("tradingagents.dataflows.news_support.requests.request", request)
    monkeypatch.setattr("tradingagents.dataflows.news_support._sleep", lambda seconds: None)
    set_config({"news_anysearch_supplement": False, "news_http_max_retries": 0})

    # Force the RSS adapter to fail so the router reaches AnySearch, then a
    # second direct RSS call proves the cache is separate from that failure.
    def broken_rss(*args, **kwargs):
        raise VendorUnavailableError("Google News RSS HTTP 503")

    _chain(monkeypatch, "get_news", {"google_news": broken_rss})
    failed = router.route_to_vendor("get_news", "AMD", today, today)
    assert failed.startswith("DATA_UNAVAILABLE")
    assert secret not in failed and password not in failed
    assert calls["any"] == 1

    # Restore the real RSS vendor and fetch twice: one HTTP GET, then cache.
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "google_news", get_google_news)
    # The previous 402 armed AnySearch backoff. RSS must still answer.
    first = router.route_to_vendor("get_news", "AMD", today, today)
    second = router.route_to_vendor("get_news", "AMD", today, today)
    assert "Router AMD" in first and first == second
    assert calls["rss"] == 1
    assert secret not in first
