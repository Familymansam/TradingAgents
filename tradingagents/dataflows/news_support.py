"""Shared news-vendor helpers: dates, dedup, cache, bounded HTTP, and report text.

Google News RSS and AnySearch both speak through these helpers so a publication
time is either taken from the source or left unknown. Nothing here invents a
date, and a cached payload never includes an API key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

USER_AGENT = "tradingagents/0.7.1 (+https://github.com/Familymansam/TradingAgents; personal news reader)"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_memory: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()

# Topic-constrained macro queries. Google News receives every query with an
# after:/before: window. AnySearch uses the first ``anysearch_macro_query_limit``
# of them as the query text on finance.news type=general (the documented
# "global English financial news" constraint). Yahoo keeps its own
# ``global_news_queries`` list.
DEFAULT_MACRO_QUERIES = (
    '(Federal Reserve OR FOMC) ("interest rate" OR inflation OR "monetary policy")',
    '("stock market" OR "S&P 500") (earnings OR GDP OR "economic outlook")',
    '(tariff OR sanctions OR "trade war") (market OR economy OR stocks)',
    '(ECB OR "Bank of England" OR "Bank of Japan") ("central bank" OR "interest rate")',
    '(oil OR crude OR OPEC) (price OR supply OR energy) (market OR economy)',
)


@dataclass
class NewsItem:
    """One headline. ``published_at`` is UTC when the source gave a time, else None."""

    title: str
    url: str
    publisher: str
    snippet: str
    published_at: datetime | None
    attribution: str
    publisher_site: str = ""


def redact(text: str, secret: str = "") -> str:
    """Drop a secret and any Bearer token from text that might be logged."""
    if not text:
        return ""
    cleaned = text
    if secret:
        cleaned = cleaned.replace(secret, "***")
    return re.sub(r"Bearer\s+\S+", "Bearer ***", cleaned)


def clear_news_cache() -> None:
    """Drop the in-process news cache. File entries expire by their own TTL."""
    with _lock:
        _memory.clear()


def _sleep(seconds: float) -> None:
    """Indirection so tests can skip the backoff wait."""
    if seconds > 0:
        time.sleep(seconds)


def _cache_ttl() -> float:
    try:
        return max(0.0, float(get_config().get("news_cache_ttl_seconds", 900)))
    except (TypeError, ValueError):
        return 900.0


def _cache_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    root = Path(get_config().get("data_cache_dir") or ".") / "news"
    return root / digest[:2] / f"{digest}.json"


def cache_get(key: str) -> str | None:
    """Return a cached payload, or None when the entry is missing or stale."""
    ttl = _cache_ttl()
    now = time.time()
    with _lock:
        hit = _memory.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    path = _cache_path(key)
    try:
        if path.exists() and now - path.stat().st_mtime < ttl:
            text = path.read_text(encoding="utf-8")
        else:
            return None
    except OSError:
        return None
    with _lock:
        _memory[key] = (now, text)
    return text


def cache_set(key: str, text: str) -> None:
    """Remember a successful public payload. Callers must not pass secrets."""
    now = time.time()
    with _lock:
        _memory[key] = (now, text)
    path = _cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("News cache write failed: %s", type(exc).__name__)


def http_request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float,
    max_retries: int,
    secret: str = "",
) -> requests.Response:
    """One HTTP call with a bounded retry on transport errors, 429, and 5xx.

    The response is returned even when the status is an error, so the caller
    can decide what it means. Error bodies are not logged: an AnySearch 402
    can contain a generated key. ``secret``, when passed, is stripped from the
    transport-error message.
    """
    attempts = max(0, int(max_retries)) + 1
    host = urllib.parse.urlparse(url).netloc
    last_transport: str | None = None
    for attempt in range(attempts):
        try:
            response = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_transport = redact(f"{type(exc).__name__}: {exc}", secret)
            logger.warning(
                "News HTTP %s %s transport failure (attempt %s/%s)",
                method, host, attempt + 1, attempts,
            )
            if attempt + 1 < attempts:
                _sleep(min(8.0, 0.4 * (2 ** attempt)))
                continue
            raise VendorUnavailableError(f"HTTP request failed: {last_transport}") from None
        if response.status_code in _RETRY_STATUSES and attempt + 1 < attempts:
            wait = _retry_after_seconds(response.headers)
            if wait is None:
                wait = min(8.0, 0.4 * (2 ** attempt))
            logger.warning(
                "News HTTP %s %s status %s; retrying in %.1fs (attempt %s/%s)",
                method, host, response.status_code, wait, attempt + 1, attempts,
            )
            _sleep(wait)
            continue
        if response.status_code < 400 and len(response.content) > MAX_RESPONSE_BYTES:
            raise VendorUnavailableError(
                f"HTTP response from {host} exceeded {MAX_RESPONSE_BYTES} bytes"
            )
        return response
    raise VendorUnavailableError(f"HTTP request failed: {last_transport or host}")


def _retry_after_seconds(headers) -> float | None:
    """Seconds from a Retry-After header, capped at 8 so a run does not stall.

    Returns None when the header is missing or not a delay in seconds (HTTP-date
    values are left to the caller's own short backoff). A valid ``0`` stays 0.
    """
    try:
        raw = headers.get("Retry-After") if headers else None
        return min(float(raw), 8.0) if raw is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def http_timeout() -> float:
    try:
        return max(1.0, float(get_config().get("news_http_timeout_seconds", 12)))
    except (TypeError, ValueError):
        return 12.0


def http_retries() -> int:
    try:
        return max(0, int(get_config().get("news_http_max_retries", 1)))
    except (TypeError, ValueError):
        return 1


def snippet_chars() -> int:
    try:
        return max(80, int(get_config().get("news_snippet_chars", 480)))
    except (TypeError, ValueError):
        return 480


def macro_queries() -> list[str]:
    configured = get_config().get("news_macro_queries")
    if isinstance(configured, (list, tuple)) and configured:
        return [str(q).strip() for q in configured if str(q).strip()]
    return list(DEFAULT_MACRO_QUERIES)


def parse_http_date(value) -> datetime | None:
    """RFC 822 / ISO-8601 to UTC, or None when the value is missing or unusable.

    A naive ISO value is read as UTC. This never guesses a date from a headline.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse_rfc822(text)
    if parsed is None:
        parsed = _parse_iso(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_rfc822(text: str) -> datetime | None:
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _parse_iso(text: str) -> datetime | None:
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def http_url(value: str) -> str:
    """An http(s) URL, or "" when the value is not one."""
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value.strip()
    return ""


def publisher_host(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


def dedup(items: list[NewsItem]) -> list[NewsItem]:
    """Drop later items with the same URL, or the same title and publisher."""
    seen: set[tuple[str, ...]] = set()
    kept: list[NewsItem] = []
    for item in items:
        url = item.url.split("#", 1)[0].rstrip("/").lower()
        title = " ".join(item.title.lower().split())
        publisher = item.publisher.lower()
        ident = ("u", url) if url else ("t", title, publisher)
        if ident in seen:
            continue
        seen.add(ident)
        kept.append(item)
    return kept


def cap_items(items: list[NewsItem], limit: int) -> list[NewsItem]:
    """Newest dated items first, then undated items, up to ``limit``."""
    dated = sorted(
        (item for item in items if item.published_at is not None),
        key=lambda item: item.published_at,
        reverse=True,
    )
    undated = [item for item in items if item.published_at is None]
    return (dated + undated)[: max(0, limit)]


def render_news(heading: str, items: list[NewsItem]) -> str:
    """A prompt block that keeps known and unknown publication times apart."""
    dated = [item for item in items if item.published_at is not None]
    undated = [item for item in items if item.published_at is None]
    note = (
        "Headlines and snippets only, not full text. A publication time is printed "
        "only when that source provided one; dates are not inferred from the headline. "
        "Undated AnySearch rows are live search hits and are not historical validated evidence."
    )
    parts = [
        heading,
        note,
        f"Dated items: {len(dated)}. Undated items: {len(undated)}.",
    ]
    if dated:
        parts.append("Dated items (publication time known):\n\n" + _blocks(dated))
    if undated:
        parts.append(
            "Undated items (publication time unknown; not historical validated evidence):\n\n"
            + _blocks(undated)
        )
    return "\n\n".join(parts).rstrip() + "\n"


def _blocks(items: list[NewsItem]) -> str:
    limit = snippet_chars()
    blocks = []
    for item in items:
        if item.published_at is None:
            published = (
                "Published: unknown (publication time was not provided; "
                "not historical validated evidence)"
            )
        else:
            published = (
                "Published: "
                + item.published_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
                + " (publication time known)"
            )
        snippet = " ".join((item.snippet or "").split())
        if len(snippet) > limit:
            snippet = snippet[: limit - 1].rstrip() + "…"
        lines = [
            f"### {item.title}",
            f"Publisher: {item.publisher or 'unknown'}",
            published,
            "Form: headline and snippet only, not the full article.",
            f"Attribution: {item.attribution}",
            f"Snippet: {snippet or 'unavailable'}",
            f"Link: {item.url or 'unavailable'}",
        ]
        if item.publisher_site:
            lines.append(f"Publisher site: {item.publisher_site}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def dump_items(items: list[NewsItem]) -> str:
    return json.dumps(
        [
            {
                "title": item.title,
                "url": item.url,
                "publisher": item.publisher,
                "snippet": item.snippet,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "attribution": item.attribution,
                "publisher_site": item.publisher_site,
            }
            for item in items
        ],
        ensure_ascii=False,
    )


def load_items(payload: str) -> list[NewsItem]:
    raw = json.loads(payload)
    items = []
    for row in raw:
        published = parse_http_date(row.get("published_at"))
        items.append(
            NewsItem(
                title=row.get("title") or "",
                url=row.get("url") or "",
                publisher=row.get("publisher") or "",
                snippet=row.get("snippet") or "",
                published_at=published,
                attribution=row.get("attribution") or "",
                publisher_site=row.get("publisher_site") or "",
            )
        )
    return items
