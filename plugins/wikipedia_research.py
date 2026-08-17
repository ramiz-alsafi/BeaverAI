"""
Beaver Plugin — Wikipedia Research
====================================

Fast, keyless knowledge lookups via Wikipedia's public REST API.
Great for standard, researcher, and seo personas — instant factual
context without spending a web-search budget.

No API key required.  httpx is already in core requirements.txt.

Activate
--------
  Just drop this file in plugins/ and restart Beaver.
  ENABLED is True out of the box — no pip installs needed.

Tools
-----
  wikipedia_search  — find an article by query, return summary sentences
  wikipedia_get_page — fetch the full introduction section by exact title
"""
import httpx
from langchain_core.tools import tool

PERSONA  = ["standard", "seo", "orchestrator", "coder"]
ENABLED  = False

_WIKI_SEARCH_URL  = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
_WIKI_OPENSEARCH  = "https://en.wikipedia.org/w/api.php"
_TIMEOUT          = 10.0
_UA               = "Beaver-Agent/2.0 (httpx; research plugin)"


def _pick_best_title(query: str) -> str | None:
    """Run OpenSearch to find the closest Wikipedia title for a free-text query."""
    params = {
        "action": "opensearch",
        "search": query,
        "limit":  3,
        "namespace": 0,
        "format": "json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            r = client.get(_WIKI_OPENSEARCH, params=params)
            r.raise_for_status()
            data = r.json()
            # data[1] is the list of title suggestions
            titles = data[1] if len(data) > 1 else []
            return titles[0] if titles else None
    except Exception as exc:
        return None


def _get_summary(title: str) -> dict:
    """Call the Wikipedia REST summary endpoint for a canonical title."""
    url = _WIKI_SEARCH_URL.format(httpx.URL(title).path or title)
    # The REST endpoint wants the title URL-encoded in the path
    encoded = str(httpx.URL("https://en.wikipedia.org/api/rest_v1/page/summary/" + title.replace(" ", "_")))
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True) as client:
        r = client.get(encoded)
        r.raise_for_status()
        return r.json()


@tool
def wikipedia_search(query: str, sentences: int = 4) -> str:
    """Search Wikipedia for a topic and return a concise summary.

    Uses OpenSearch to find the best article match, then returns the
    first N sentences of its summary together with the article URL.
    Ideal for quick factual context on people, places, concepts, events.

    Parameters
    ----------
    query     : free-text search query (e.g. "LangGraph agent framework")
    sentences : number of summary sentences to return (default 4, max 10)
    """
    sentences = min(max(1, sentences), 10)

    title = _pick_best_title(query)
    if not title:
        return f"[Wikipedia] No article found for query: '{query}'"

    try:
        data    = _get_summary(title)
        extract = data.get("extract", "")
        url     = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        page_title = data.get("title", title)
        desc    = data.get("description", "")

        # Trim to the requested number of sentences (split on ". ")
        sents = [s.strip() for s in extract.replace("\n", " ").split(". ") if s.strip()]
        trimmed = ". ".join(sents[:sentences])
        if not trimmed.endswith("."):
            trimmed += "."

        parts = [f"📖 **{page_title}**"]
        if desc:
            parts.append(f"_{desc}_")
        parts.append("")
        parts.append(trimmed)
        if url:
            parts.append(f"\n🔗 {url}")

        return "\n".join(parts)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"[Wikipedia] No article found for: '{title}'"
        return f"[Wikipedia] HTTP error {e.response.status_code} fetching '{title}'"
    except Exception as exc:
        return f"[Wikipedia] Error: {exc}"


@tool
def wikipedia_get_page(title: str, max_chars: int = 4000) -> str:
    """Fetch the introduction section of a Wikipedia article by its exact title.

    Use this when wikipedia_search found the right article but you need
    more depth than the summary — the introduction section typically
    covers the definition, history, and main sub-topics.

    Parameters
    ----------
    title     : exact Wikipedia article title (e.g. "Large language model")
    max_chars : truncate output to this many characters (default 4000)
    """
    max_chars = min(max(500, max_chars), 8000)

    try:
        data    = _get_summary(title)
        extract = data.get("extract", "")
        url     = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        page_title = data.get("title", title)
        desc    = data.get("description", "")

        if not extract:
            return f"[Wikipedia] '{title}' exists but has no extractable text."

        body = extract.replace("\n\n", "\n").strip()
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n[... truncated — use smaller max_chars or narrow your query ...]"

        parts = [f"📖 **{page_title}**"]
        if desc:
            parts.append(f"_{desc}_")
        parts.append("")
        parts.append(body)
        if url:
            parts.append(f"\n🔗 {url}")

        return "\n".join(parts)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return (
                f"[Wikipedia] Article '{title}' not found.\n"
                "Tip: use wikipedia_search first to find the exact title."
            )
        return f"[Wikipedia] HTTP error {e.response.status_code}"
    except Exception as exc:
        return f"[Wikipedia] Error: {exc}"


TOOLS = [wikipedia_search, wikipedia_get_page]
