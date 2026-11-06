# Google News RSS and AnySearch news

This fork reads `get_news` (one ticker) and `get_global_news` (macro) from Google News RSS, then from AnySearch when a window reaches today. Yahoo Finance news and Alpha Vantage stay registered. They are not called unless a chain names them. Price, indicator, fundamental, and insider vendors are unchanged.

The patch is against fork `main` at `9fa1196` (TradingAgents 0.7.1). A parent checkout that has diverged, or that carries a local edit to `tradingagents/dataflows/vendors/yahoo/news.py`, must not be overwritten with this tree.

## What each source is allowed to claim

| Source | What is stored | Publication time | Historical window |
| --- | --- | --- | --- |
| Google News RSS | Headline, publisher, snippet when the feed has one, Google News link | `pubDate`, converted to UTC. Missing or unparseable means unknown. | Kept only when the UTC timestamp falls in `[start, end + 1 day)`. |
| AnySearch `finance.news` | Headline, snippet, result URL. The `content` field is not copied into the report. | No reliable `published_at`. A timestamp is shown only when the payload actually contains one that parses. | Not called. Undated rows are not historical evidence. |

Nothing in this path invents a date from a headline or a snippet. An empty feed, a transport error, and a quota error are reported as unavailable, not as "no news happened."

Google News RSS is a recent sample, not an archive. A past window the feed does not reach is an unavailable window.

## Vendor chain

`tool_vendors` overrides the category default:

```python
"get_news": "google_news,anysearch",
"get_global_news": "google_news,anysearch",
```

`data_vendors["news_data"]` stays `yfinance` so insider transactions still use Yahoo. To try Yahoo news only after the new sources fail, set the chain to `google_news,anysearch,yfinance`. To use the local Yahoo news patch only, set both tools to `yfinance`.

Do not add `alpha_vantage` for these tools. `NEWS_SENTIMENT` is a premium endpoint.

On a window that reaches today, Google News RSS also calls AnySearch and appends rows it did not already return (`news_anysearch_supplement`, default true). That call is skipped when `ANYSEARCH_API_KEY` is unset. Set `news_anysearch_supplement` to false to keep AnySearch as router fallback only (RSS failure, then one AnySearch call).

## AnySearch request

`POST https://api.anysearch.com/v1/search` with `Authorization: Bearer $ANYSEARCH_API_KEY`.

The finance catalog (read from `GET /v1/sub-domains?domain=finance` on 2026-11-06; no API key) defines `finance.news` as:

- `type=stock` and `symbol` for one international ticker. Query text is `{SYMBOL} stock news`.
- `type=general` for global English financial news. There is no topic parameter. Macro calls send the configured topic query as `query` and `type=general`, which is the documented English-finance constraint. Untagged general search is not used.
- `period` is a live lookback such as `7d` or `{N}d`, capped by `anysearch_max_period_days` (default 14). It is not an as-of historical filter.
- `language=en` and `zone=intl` are the documented top-level fields.
- `max_results` is clamped to 1–10.

Macro AnySearch calls use the first `anysearch_macro_query_limit` entries of `news_macro_queries` (default 2) so one report does not spend the whole quota. Google News RSS runs every query in that list, with `after:` / `before:` added. `before:` is the day after `end_date`; the `pubDate` filter is what enforces the window. Yahoo still uses `global_news_queries`, which this change does not rewrite.

`finance.macro` is an indicator feed, not news, and is not called. `type=flash` and `type=announcement` are Chinese-source feeds and are not called.

## Failure, cache, and secrets

- Timeout default 12 seconds. One retry after the first attempt on transport errors, HTTP 429, and HTTP 5xx. The wait is at most 8 seconds and honors a numeric `Retry-After` up to that cap.
- HTTP 402 and a 429 that survives the retry arm a process-local backoff (default 60 seconds, cap 120). Later AnySearch calls in that process fail immediately with `quota backoff` and do not send another request.
- Successful public payloads are cached under `data_cache_dir/news` for `news_cache_ttl_seconds` (default 900). The cache key is the query, not the API key. A body that contains the key is not cached.
- Logs and tool text for AnySearch errors contain the HTTP status and `X-Request-ID` only. A 402 body can contain a generated password and key; it is not read into the report or the log.
- The key is read from `ANYSEARCH_API_KEY` at call time. It is not written into config, reports, or this repository.

## Configure

In the environment that runs TradingAgents, not in git:

```bash
export ANYSEARCH_API_KEY=...   # parent box only
```

Leave it unset to run on Google News RSS alone. Restart the process after changing the key or `tool_vendors`.

## Tests

Unit tests mock HTTP. They cover bad RSS XML, a refused DTD, window filters, a `+0500` timestamp, undated RSS rows, AnySearch schema (`content` dropped), auth 401, HTTP 502, HTTP 402/429 without the body, unknown and future dates, historical withholding, router fallback, cache TTL, and redaction of a fake key.

`pytest -m integration tests/test_google_news_rss.py` fetches live Google News RSS for AMD and one macro query. AnySearch is not called in that test. Authenticated AnySearch is for the parent box to live-test; this workspace does not have the key and does not request it.

## Deploy on the parent box

The production tree is the upstream checkout plus a local Yahoo news edit. Preserve both of these files. Do not delete the backup, and do not `git checkout` a clean copy over them:

- `tradingagents/dataflows/vendors/yahoo/news.py`
- `tradingagents/dataflows/vendors/yahoo/news.py.bak_20261103`

This patch does not modify those paths. Do not assume the parent layout matches this fork. If `git apply --check` fails, stop and use the manual file list below. Do not apply with `--reject` and then guess.

### 1. Save the local Yahoo news files

```bash
cd /workspace/TradingAgents
git rev-parse HEAD > /tmp/tradingagents-parent-head.txt
git diff -- tradingagents/dataflows/vendors/yahoo/news.py > /tmp/yahoo-news.parent.patch
cp -a tradingagents/dataflows/vendors/yahoo/news.py /tmp/yahoo-news.py.parent
cp -a tradingagents/dataflows/vendors/yahoo/news.py.bak_20261103 /tmp/yahoo-news.py.bak_20261103
```

Confirm the backup copy is non-empty before continuing:

```bash
test -s /tmp/yahoo-news.py.parent && test -s /tmp/yahoo-news.py.bak_20261103
```

### 2. Apply the fork patch

Copy `deploy/google-news-anysearch.patch` from this fork onto the parent box. From the production repo root:

```bash
git apply --check deploy/google-news-anysearch.patch
git apply deploy/google-news-anysearch.patch
cmp tradingagents/dataflows/vendors/yahoo/news.py /tmp/yahoo-news.py.parent
cmp tradingagents/dataflows/vendors/yahoo/news.py.bak_20261103 /tmp/yahoo-news.py.bak_20261103
```

`cmp` must print nothing. If either file differs, restore it from `/tmp` before starting the process:

```bash
cp -a /tmp/yahoo-news.py.parent tradingagents/dataflows/vendors/yahoo/news.py
cp -a /tmp/yahoo-news.py.bak_20261103 tradingagents/dataflows/vendors/yahoo/news.py.bak_20261103
```

### 3. If the patch does not apply

The parent is not at the same base as this fork. Copy these new files in place (they only import modules that already exist: `config`, `date_window`, `errors`, and `requests`):

- `tradingagents/dataflows/news_support.py`
- `tradingagents/dataflows/vendors/google_news/__init__.py`
- `tradingagents/dataflows/vendors/google_news/news.py`
- `tradingagents/dataflows/vendors/anysearch/__init__.py`
- `tradingagents/dataflows/vendors/anysearch/news.py`

Then register them by hand. In `tradingagents/dataflows/router.py` add:

```python
from tradingagents.dataflows.vendors.anysearch.news import (
    get_global_news as get_anysearch_global_news,
    get_news as get_anysearch_news,
)
from tradingagents.dataflows.vendors.google_news.news import (
    get_global_news as get_google_global_news,
    get_news as get_google_news,
)
```

In `VENDOR_METHODS["get_news"]` and `VENDOR_METHODS["get_global_news"]`, add `"google_news"` and `"anysearch"` ahead of `"yfinance"`. Leave `get_insider_transactions`, `get_stock_data`, and the fundamental methods as they are.

In the config the process actually loads, set:

```python
"tool_vendors": {
    "get_news": "google_news,anysearch",
    "get_global_news": "google_news,anysearch",
},
```

The other keys (`news_macro_queries`, timeouts, cache TTL, AnySearch URL) have defaults inside the new modules, so a partial config still runs. Copy them from `tradingagents/default_config.py` in this fork if you want them editable.

Do not replace `tradingagents/dataflows/vendors/yahoo/news.py`.

### 4. Install and point the key at the process

```bash
pip install -e .
# ANYSEARCH_API_KEY belongs in the parent environment or its untracked .env.
# Do not commit it and do not paste it into the patch.
```

Restart the process that imports `tradingagents`. A remote test is not a deployment until this process is the one that serves the workflow.

### 5. Live check on the parent

Use the parent's environment. This confirms RSS dates and that AnySearch rows are labeled undated. It does not print the key.

```bash
python - <<'PY'
import os
from datetime import date, timedelta
from tradingagents.dataflows.vendors.google_news.news import get_news, get_global_news

assert "ANYSEARCH_API_KEY" in os.environ and os.environ["ANYSEARCH_API_KEY"].strip()
today = date.today()
start = today - timedelta(days=3)
ticker = get_news("AMD", start.isoformat(), today.isoformat())
macro = get_global_news(today.isoformat(), 3, 5)
key = os.environ["ANYSEARCH_API_KEY"]
for name, text in ("ticker", ticker), ("macro", macro):
    assert key not in text, name
    print("---", name, "chars", len(text))
    print(text[:1500])
PY
```

Expect `publication time known` on RSS rows and, when AnySearch returns rows, `Published: unknown` plus `not historical validated evidence`. A historical date should not contain AnySearch rows:

```bash
python - <<'PY'
from tradingagents.dataflows.vendors.google_news.news import get_news
text = get_news("AMD", "2024-06-01", "2024-06-07")
assert "AnySearch" not in text
print(text[:800])
PY
```

## Rollback

From the production repo root, if the patch applied cleanly:

```bash
git apply -R --check deploy/google-news-anysearch.patch
git apply -R deploy/google-news-anysearch.patch
cmp tradingagents/dataflows/vendors/yahoo/news.py /tmp/yahoo-news.py.parent
cmp tradingagents/dataflows/vendors/yahoo/news.py.bak_20261103 /tmp/yahoo-news.py.bak_20261103
```

If `git apply -R` cannot reverse a hand-merge, delete only the five new files listed above and remove the `google_news` / `anysearch` registrations and the `tool_vendors` overrides you added. Then:

```bash
cp -a /tmp/yahoo-news.py.parent tradingagents/dataflows/vendors/yahoo/news.py
cp -a /tmp/yahoo-news.py.bak_20261103 tradingagents/dataflows/vendors/yahoo/news.py.bak_20261103
```

Restart the process. Unset `ANYSEARCH_API_KEY` only if you also want the process to stop presenting it to anything else; rollback of this code does not require deleting the key.

Do not roll back by checking out upstream `tradingagents/dataflows/vendors/yahoo/news.py`. That drops the local patch saved in `/tmp/yahoo-news.py.parent`.
