"""
Beaver Plugin — Web Search
===========================

Broad web search and page fetching for any persona.
Replaces the old wikipedia_research plugin.

Requires:
    pip install duckduckgo-search   (already commented in requirements.txt — uncomment it)

Tools
-----
  web_search   — full-text DuckDuckGo search, returns titles + URLs + snippets
  web_fetch    — fetch a page and return readable plain text (strips HTML)
  ddg_news     — DuckDuckGo news search, returns recent articles
"""

import re
import httpx
from langchain_core.tools import tool

PERSONA  = ["standard", "seo", "orchestrator", "coder", "researcher", "pentester", "social"]
ENABLED  = True   # requires: pip install duckduckgo-search

_TIMEOUT = 15.0
_UA      = "Mozilla/5.0 (Beaver-Agent/2.0; research)"
_MAX_BODY = 8_000   # chars — prevents context flooding


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Very lightweight HTML → plain text: remove tags, decode common entities."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>",  " ", text,  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for ent, ch in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&nbsp;"," "),("&quot;",'"')]:
        text = text.replace(ent, ch)
    return re.sub(r"\s{2,}", " ", text).strip()


def _ddgs():
    """Return a DDGS instance or raise ImportError with actionable message."""
    try:
        from duckduckgo_search import DDGS
        return DDGS()
    except ImportError:
        raise ImportError(
            "web_search plugin requires duckduckgo-search.\n"
            "Fix: pip install duckduckgo-search"
        )


# ── Tools ──────────────────────────────────────────────────────────────────────

@tool
def web_search(query: str, max_results: int = 6) -> str:
    """Search the web using DuckDuckGo and return the top results.

    Returns a numbered list of results, each with title, URL, and a
    short snippet.  Use web_fetch() to read the full content of any URL.

    Parameters
    ----------
    query       : search query string
    max_results : number of results to return (default 6, capped at 10)
    """
    max_results = min(max(1, max_results), 10)
    try:
        with _ddgs() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results))
    except ImportError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        return f"[web_search] Search failed: {exc}"

    if not hits:
        return f"[web_search] No results found for: '{query}'"

    lines = [f"Search results for '{query}':\n"]
    for i, r in enumerate(hits, 1):
        lines.append(f"{i}. {r.get('title', '(no title)')}")
        lines.append(f"   {r.get('href', '')}")
        snippet = r.get("body", "").strip()
        if snippet:
            lines.append(f"   {snippet[:200]}")
        lines.append("")

    return "\n".join(lines).rstrip()


@tool
def web_fetch(url: str, max_chars: int = 6000) -> str:
    """Fetch the readable text content of a web page.

    Strips HTML tags and returns plain text, truncated to max_chars.
    Use this after web_search() to read full articles or documentation.

    Parameters
    ----------
    url       : full URL (must start with http:// or https://)
    max_chars : truncate output to this many characters (default 6000, max 12000)
    """
    max_chars = min(max(500, max_chars), 12_000)

    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}' — must start with http:// or https://"

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": _UA}) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"[web_fetch] HTTP {exc.response.status_code} fetching {url}"
    except httpx.TimeoutException:
        return f"[web_fetch] Timeout fetching {url}"
    except Exception as exc:
        return f"[web_fetch] Error: {exc}"

    content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        text = _strip_html(resp.text)
    else:
        text = resp.text

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated — {len(text) - max_chars} more chars ...]"

    return f"[{url}]\n\n{text}"


@tool
def ddg_news(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo News for recent articles on a topic.

    Returns recent news items with title, URL, source, and date.
    Useful for current events, recent announcements, or breaking news.

    Parameters
    ----------
    query       : news search query
    max_results : number of articles to return (default 5, capped at 10)
    """
    max_results = min(max(1, max_results), 10)
    try:
        with _ddgs() as ddgs:
            articles = list(ddgs.news(query, max_results=max_results))
    except ImportError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        return f"[ddg_news] Search failed: {exc}"

    if not articles:
        return f"[ddg_news] No news found for: '{query}'"

    lines = [f"Recent news for '{query}':\n"]
    for i, a in enumerate(articles, 1):
        lines.append(f"{i}. {a.get('title', '(no title)')}")
        lines.append(f"   Source: {a.get('source', '?')}  Date: {a.get('date', '?')}")
        lines.append(f"   {a.get('url', '')}")
        body = a.get("body", "").strip()
        if body:
            lines.append(f"   {body[:180]}")
        lines.append("")

    return "\n".join(lines).rstrip()


TOOLS = [web_search, web_fetch, ddg_news]