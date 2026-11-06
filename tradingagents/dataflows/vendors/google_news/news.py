"""Google News RSS headlines for ``get_news`` and ``get_global_news``.

The public search feed is RSS 2.0. Each item carries ``title``, ``link``,
``pubDate``, and ``source``. ``pubDate`` is the publication time used for the
window. An item with no parseable ``pubDate`` is undated: kept only when the
window reaches the present, and never described as a known date.

The feed is a recent sample, not an archive. A window it does not reach is
reported as unavailable rather than as an absence of news. AnySearch, when
configured, is appended only for a window that reaches today; its rows stay
undated and are not treated as historical evidence. A transport or parse
failure raises ``VendorUnavailableError`` so the router can try the next vendor.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.errors import VendorError, VendorUnavailableError
from tradingagents.dataflows.news_support import (
    USER_AGENT,
    NewsItem,
    cache_get,
    cache_set,
    cap_items,
    dedup,
    dump_items,
    http_request,
    http_retries,
    http_timeout,
    http_url,
    load_items,
    macro_queries,
    parse_http_date,
    render_news,
)

logger = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search"
_SYMBOL = re.compile(r"[A-Za-z0-9._^=+\-]{1,32}")
_TAG = re.compile(r"<[^>]+>")


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Headlines for ``ticker`` in ``[start_date, end_date]``, Google News first."""
    symbol = _news_symbol(ticker)
    items = _search(_ticker_query(symbol, start_date, end_date))
    return _report(
        items,
        start_date,
        end_date,
        heading=f"## {ticker} News, from {start_date} to {end_date}:",
        subject=f"news for {ticker}",
        limit=int(get_config().get("news_article_limit", 20)),
        supplement=_supplement_ticker(ticker, start_date, end_date),
    )


def get_global_news(
    as_of_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Macro headlines in the lookback ending on ``as_of_date``."""
    config = get_config()
    if look_back_days is None:
        look_back_days = int(config.get("global_news_lookback_days", 7))
    if limit is None:
        limit = int(config.get("global_news_article_limit", 10))
    start_dt = datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=int(look_back_days))
    start_date = start_dt.strftime("%Y-%m-%d")
    found: list[NewsItem] = []
    for topic in macro_queries():
        found.extend(_search(_macro_query(topic, start_date, as_of_date)))
        kept = _in_window(dedup(found), start_date, as_of_date)
        if len(kept) >= limit:
            break
    return _report(
        found,
        start_date,
        as_of_date,
        heading=f"## Global Market News, from {start_date} to {as_of_date}:",
        subject="market news",
        limit=limit,
        supplement=_supplement_macro(as_of_date, int(look_back_days), limit),
    )


def _report(
    fetched: list[NewsItem],
    start_date: str,
    end_date: str,
    *,
    heading: str,
    subject: str,
    limit: int,
    supplement: list[NewsItem],
) -> str:
    in_range = _in_window(dedup(fetched), start_date, end_date)
    extra = _new_items(supplement, in_range)
    chosen = cap_items(in_range + extra, limit)
    if chosen:
        return render_news(heading, chosen)
    dates = [item.published_at for item in fetched if item.published_at is not None]
    gap = coverage_gap(dates, start_date, end_date, "Google News RSS", subject)
    return gap or f"No news found between {start_date} and {end_date}"


def _in_window(items: list[NewsItem], start_date: str, end_date: str) -> list[NewsItem]:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    return [item for item in items if in_window(item.published_at, start_dt, end_dt)]


def _new_items(extra: list[NewsItem], have: list[NewsItem]) -> list[NewsItem]:
    """Supplemental rows whose URL or title is not already in the RSS sample."""
    seen_urls = {item.url.split("#", 1)[0].rstrip("/").lower() for item in have if item.url}
    seen_titles = {" ".join(item.title.lower().split()) for item in have}
    fresh = []
    for item in extra:
        url = item.url.split("#", 1)[0].rstrip("/").lower()
        title = " ".join(item.title.lower().split())
        if (url and url in seen_urls) or title in seen_titles:
            continue
        fresh.append(item)
    return fresh


def _supplement_ticker(ticker: str, start_date: str, end_date: str) -> list[NewsItem]:
    if not _supplement_enabled(end_date):
        return []
    from tradingagents.dataflows.vendors.anysearch.news import search_ticker

    return _supplement(lambda: search_ticker(ticker, start_date, end_date))


def _supplement_macro(as_of_date: str, look_back_days: int, limit: int) -> list[NewsItem]:
    if not _supplement_enabled(as_of_date):
        return []
    from tradingagents.dataflows.vendors.anysearch.news import search_macro

    return _supplement(lambda: search_macro(as_of_date, look_back_days, limit))


def _supplement_enabled(end_date: str) -> bool:
    """AnySearch is live-only. A past window cannot be validated without a date."""
    if not get_config().get("news_anysearch_supplement", True):
        return False
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    return in_window(None, end_dt, end_dt)


def _supplement(fetch) -> list[NewsItem]:
    try:
        return fetch()
    except VendorError as exc:
        logger.warning("AnySearch supplement unavailable: %s", type(exc).__name__)
        return []


def _news_symbol(ticker: str) -> str:
    if not isinstance(ticker, str) or not _SYMBOL.fullmatch(ticker.strip()):
        raise VendorUnavailableError("ticker is not a usable news symbol")
    return ticker.strip().upper()


def _ticker_query(symbol: str, start_date: str, end_date: str) -> str:
    # before: is the day after end_date so the end date itself can match.
    # pubDate filtering is what actually enforces the window.
    before = _day_after(end_date)
    return (
        f'"{symbol}" (stock OR shares OR earnings OR company) '
        f"after:{start_date} before:{before}"
    )


def _macro_query(topic: str, start_date: str, end_date: str) -> str:
    # Do not wrap ``topic`` again. Google News drops a query that is already
    # parenthesized when another pair of parentheses is added in front of
    # after:/before:.
    return f"{topic} after:{start_date} before:{_day_after(end_date)}"


def _day_after(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _search(query: str) -> list[NewsItem]:
    config = get_config()
    params = {
        "q": query,
        "hl": config.get("google_news_hl") or "en-US",
        "gl": config.get("google_news_gl") or "US",
        "ceid": config.get("google_news_ceid") or "US:en",
    }
    cache_key = "google-news:" + urllib.parse.urlencode(params)
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return load_items(cached)
        except (ValueError, TypeError, KeyError):
            logger.warning("Ignoring an unreadable Google News cache entry")
    xml_text = _fetch(params)
    items = parse_feed(xml_text)
    cache_set(cache_key, dump_items(items))
    return items


def _fetch(params: dict) -> str:
    response = http_request(
        "GET",
        RSS_URL,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"},
        timeout=http_timeout(),
        max_retries=http_retries(),
    )
    if response.status_code != 200:
        raise VendorUnavailableError(f"Google News RSS HTTP {response.status_code}")
    return response.content.decode("utf-8", errors="replace")


def parse_feed(xml_text: str) -> list[NewsItem]:
    """RSS 2.0 or Atom items. Bad XML is an outage, not an empty news day."""
    if "<!ENTITY" in xml_text.upper():
        raise VendorUnavailableError("Google News RSS contained a DTD entity and was refused")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise VendorUnavailableError("Google News RSS was not valid XML") from None
    items = []
    for node in root.iter():
        kind = node.tag.rsplit("}", 1)[-1]
        if kind not in {"item", "entry"}:
            continue
        item = _item_from(node)
        if item is not None:
            items.append(item)
    return items


def _item_from(node: ET.Element) -> NewsItem | None:
    title = _text(node, "title")
    if not title:
        return None
    link = _link(node)
    published = parse_http_date(_text(node, "pubDate") or _text(node, "published") or _text(node, "updated"))
    source_name, source_url = _source(node)
    snippet = _snippet(_text(node, "description") or _text(node, "summary") or _text(node, "content"))
    return NewsItem(
        title=title,
        url=link,
        publisher=source_name or "Google News",
        snippet=snippet,
        published_at=published,
        attribution="Google News RSS",
        publisher_site=source_url,
    )


def _text(node: ET.Element, name: str) -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == name and child.text:
            return " ".join(child.text.split())
    return ""


def _link(node: ET.Element) -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        href = http_url(child.get("href") or "") or http_url(child.text or "")
        if href:
            return href
    return ""


def _source(node: ET.Element) -> tuple[str, str]:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] != "source":
            continue
        name = " ".join((child.text or "").split())
        return name, http_url(child.get("url") or "")
    return "", ""


def _snippet(raw: str) -> str:
    if not raw:
        return ""
    text = " ".join(html.unescape(_TAG.sub(" ", raw)).split())
    if text.startswith("http://") or text.startswith("https://"):
        return ""
    return text
