"""AnySearch ``finance.news`` for ticker and macro headlines.

Official contract (https://www.anysearch.com/docs/api-endpoints/v1-search and the
finance capability catalog): ``POST /v1/search`` with ``Authorization: Bearer``
from ``ANYSEARCH_API_KEY``. ``finance.news`` requires ``params.type``:

- ``stock`` — international single-stock news; ``symbol`` is the ticker
- ``general`` — global English financial news

``period`` is a live lookback (``7d``, ``14d``, or a custom ``{N}d``), the same
values as ``finance.quote``. It is not a historical as-of window. The success
payload's documented fields are ``title``, ``url``, ``snippet``, and ``content``.
There is no reliable ``published_at``. ``content`` is not copied into the report:
the analyst sees the headline, the snippet, and the link.

Undated rows are withheld for a window that does not reach the present. They
are not historical validated evidence, and this module does not call the API
for those windows (a live result would be later than the analysis date).

HTTP 402 and 429 back off without reading the body into a log. A 402 message
can contain a generated key. Diagnostics keep the status and ``X-Request-ID``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.news_support import (
    USER_AGENT,
    NewsItem,
    cache_get,
    cache_set,
    cap_items,
    dedup,
    http_request,
    http_retries,
    http_timeout,
    http_url,
    publisher_host,
    render_news,
)

logger = logging.getLogger(__name__)

_SEARCH_PATH = "/v1/search"
_SYMBOL = re.compile(r"[A-Z0-9.\-]{1,15}")
_DATE_FIELDS = ("published_at", "publishedAt", "pub_date", "pubDate", "published", "date")
_backoff_until = 0.0
_backoff_lock = threading.Lock()


def clear_quota_backoff() -> None:
    """Forget a quota backoff. Tests call this so one case cannot block the next."""
    global _backoff_until
    with _backoff_lock:
        _backoff_until = 0.0


def search_ticker(ticker: str, start_date: str, end_date: str) -> list[NewsItem]:
    """Live ``finance.news`` rows for ``ticker``, or an error when none can be used."""
    _require_live(start_date, end_date)
    symbol = _symbol(ticker)
    rows = _search(
        query=f"{symbol} stock news",
        params={"type": "stock", "symbol": symbol, "period": _period(start_date, end_date)},
        limit=_max_results(get_config().get("news_article_limit", 10)),
        attribution=f"AnySearch finance.news type=stock symbol={symbol}",
    )
    return _usable(rows, start_date, end_date)


def search_macro(as_of_date: str, look_back_days: int, limit: int) -> list[NewsItem]:
    """Live ``finance.news`` ``type=general`` rows for the configured macro topics."""
    start_dt = datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=int(look_back_days))
    start_date = start_dt.strftime("%Y-%m-%d")
    _require_live(start_date, as_of_date)
    topics = _macro_topics()
    found: list[NewsItem] = []
    capped = _max_results(limit)
    for topic in topics:
        found.extend(
            _search(
                query=topic,
                params={"type": "general", "period": _period(start_date, as_of_date)},
                limit=capped,
                attribution="AnySearch finance.news type=general",
            )
        )
        if len(_usable(dedup(found), start_date, as_of_date)) >= capped:
            break
    return _usable(dedup(found), start_date, as_of_date)


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Router entry. Empty and historical calls are unavailable, not 'no news'."""
    items = search_ticker(ticker, start_date, end_date)
    if not items:
        raise VendorUnavailableError("AnySearch returned no usable news items")
    limit = int(get_config().get("news_article_limit", 10))
    return render_news(
        f"## {ticker} News, from {start_date} to {end_date}:",
        cap_items(items, limit),
    )


def get_global_news(
    as_of_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Router entry for macro headlines. Same live-only rule as ``get_news``."""
    config = get_config()
    if look_back_days is None:
        look_back_days = int(config.get("global_news_lookback_days", 7))
    if limit is None:
        limit = int(config.get("global_news_article_limit", 10))
    start_date = (
        datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=int(look_back_days))
    ).strftime("%Y-%m-%d")
    items = search_macro(as_of_date, int(look_back_days), int(limit))
    if not items:
        raise VendorUnavailableError("AnySearch returned no usable macro news items")
    return render_news(
        f"## Global Market News, from {start_date} to {as_of_date}:",
        cap_items(items, int(limit)),
    )


def _require_live(start_date: str, end_date: str) -> None:
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if in_window(None, end_dt, end_dt):
        return
    raise VendorUnavailableError(
        "AnySearch publication times are not reliable, so undated results are withheld "
        f"for {start_date}..{end_date} and are not historical validated evidence"
    )


def _usable(items: list[NewsItem], start_date: str, end_date: str) -> list[NewsItem]:
    """Keep known dates inside the window. Keep unknown dates only while it is live."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    return [item for item in items if in_window(item.published_at, start_dt, end_dt)]


def _macro_topics() -> list[str]:
    from tradingagents.dataflows.news_support import macro_queries

    try:
        count = int(get_config().get("anysearch_macro_query_limit", 2))
    except (TypeError, ValueError):
        count = 2
    topics = macro_queries()[: max(1, count)]
    if not topics:
        raise VendorUnavailableError("no macro news topics are configured")
    return topics


def _search(*, query: str, params: dict, limit: int, attribution: str) -> list[NewsItem]:
    language = (get_config().get("anysearch_language") or "").strip()
    zone = (get_config().get("anysearch_zone") or "").strip()
    payload = {"query": query, "tag": "finance.news", "params": params, "max_results": limit}
    if language:
        payload["language"] = language
    if zone:
        payload["zone"] = zone
    cache_key = "anysearch:" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return _items_from_results(json.loads(cached), attribution)
        except (ValueError, TypeError):
            logger.warning("Ignoring an unreadable AnySearch cache entry")
    body = _post(payload)
    results = _results(body)
    secret = os.environ.get("ANYSEARCH_API_KEY", "").strip()
    serialized = json.dumps(results, ensure_ascii=False)
    if secret and secret in serialized:
        logger.warning("AnySearch payload echoed the API key; the result was not cached")
    else:
        cache_set(cache_key, serialized)
    return _items_from_results(results, attribution)


def _post(payload: dict) -> dict:
    remaining = _backoff_remaining()
    if remaining > 0:
        raise VendorUnavailableError(
            f"AnySearch quota backoff active for {remaining:.0f}s"
        )
    key = _api_key()
    base = (get_config().get("anysearch_base_url") or "https://api.anysearch.com").rstrip("/")
    response = http_request(
        "POST",
        base + _SEARCH_PATH,
        json_body=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        timeout=http_timeout(),
        max_retries=http_retries(),
        secret=key,
    )
    request_id = response.headers.get("X-Request-ID") or "unknown"
    status = response.status_code
    if status in {402, 429}:
        _arm_backoff(status, response.headers)
        raise VendorUnavailableError(f"AnySearch HTTP {status}; request_id={request_id}")
    if status in {401, 403}:
        raise VendorNotConfiguredError(
            f"AnySearch auth failed HTTP {status}; request_id={request_id}"
        )
    if status != 200:
        raise VendorUnavailableError(f"AnySearch HTTP {status}; request_id={request_id}")
    try:
        body = response.json()
    except ValueError:
        raise VendorUnavailableError(
            f"AnySearch response was not JSON; request_id={request_id}"
        ) from None
    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else "invalid"
        raise VendorUnavailableError(f"AnySearch code {code}; request_id={request_id}")
    return body


def _results(body: dict) -> list:
    data = body.get("data")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise VendorUnavailableError("AnySearch response had no results array")
    return results


def _items_from_results(results: list, attribution: str) -> list[NewsItem]:
    items = []
    for row in results:
        if not isinstance(row, dict):
            continue
        title = " ".join(str(row.get("title") or "").split())
        url = http_url(str(row.get("url") or ""))
        if not title and not url:
            continue
        # ``content`` is intentionally unused: it is cleaned page text, not a
        # dated article, and the report is headline/snippet only.
        snippet = " ".join(str(row.get("snippet") or "").split())
        items.append(
            NewsItem(
                title=title or "untitled",
                url=url,
                publisher=publisher_host(url) or "AnySearch",
                snippet=snippet,
                published_at=_published_at(row),
                attribution=attribution,
            )
        )
    return items


def _published_at(row: dict):
    """A documented-or-present timestamp field, or None. Never a date from the snippet."""
    from tradingagents.dataflows.news_support import parse_http_date

    for field in _DATE_FIELDS:
        if field not in row or row[field] in (None, ""):
            continue
        return parse_http_date(row[field])
    return None


def _api_key() -> str:
    key = os.environ.get("ANYSEARCH_API_KEY", "").strip()
    if not key:
        raise VendorNotConfiguredError(
            "ANYSEARCH_API_KEY is not set; AnySearch news is not configured"
        )
    return key


def _symbol(ticker: str) -> str:
    if not isinstance(ticker, str):
        raise VendorUnavailableError("ticker is not a usable AnySearch stock symbol")
    candidate = ticker.strip().upper()
    if _SYMBOL.fullmatch(candidate):
        return candidate
    raise VendorUnavailableError("ticker is not a usable AnySearch stock symbol")


def _period(start_date: str, end_date: str) -> str:
    """Live lookback ``{N}d``, capped. This is not an as-of historical filter."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    days = max(1, (end - start).days + 1)
    try:
        cap = int(get_config().get("anysearch_max_period_days", 14))
    except (TypeError, ValueError):
        cap = 14
    return f"{max(1, min(days, max(1, cap)))}d"


def _max_results(limit) -> int:
    """AnySearch accepts 1–10. Callers asking for more are clamped, not rejected."""
    try:
        asked = int(limit)
    except (TypeError, ValueError):
        asked = 10
    try:
        ceiling = int(get_config().get("anysearch_max_results", 10))
    except (TypeError, ValueError):
        ceiling = 10
    return max(1, min(asked, ceiling, 10))


def _backoff_remaining() -> float:
    with _backoff_lock:
        return max(0.0, _backoff_until - time.monotonic())


def _arm_backoff(status: int, headers) -> None:
    """Hold further calls after quota (402) or a rate limit that outlasted the retry."""
    wait = None
    try:
        raw = headers.get("Retry-After") if headers else None
        wait = float(raw) if raw is not None else None
    except (TypeError, ValueError, AttributeError):
        wait = None
    if status == 402 or wait is None:
        try:
            wait = float(get_config().get("anysearch_quota_backoff_seconds", 60))
        except (TypeError, ValueError):
            wait = 60.0
    wait = min(max(wait, 0.0), 120.0)
    global _backoff_until
    with _backoff_lock:
        _backoff_until = max(_backoff_until, time.monotonic() + wait)
    logger.warning("AnySearch HTTP %s; backing off for %.0fs", status, wait)
