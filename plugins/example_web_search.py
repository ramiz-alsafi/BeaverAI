"""
Example Beaver Plugin — Web Search
====================================

This is a template showing how to write a Beaver plugin.
Drop this file (or any .py file) into the plugins/ directory
and it will be auto-loaded for the matching persona.

To activate this plugin:
  1. pip install duckduckgo-search
  2. Set ENABLED = True below
  3. Restart Beaver

Plugin contract
---------------
  PERSONA  : str | list[str]  — which persona(s) get these tools
  TOOLS    : list[BaseTool]   — tools to add to that persona
  ENABLED  : bool             — set False to disable without deleting the file
"""
from langchain_core.tools import tool

PERSONA = ["standard", "seo", "orchestrator"]
ENABLED = False   # ← flip to True after: pip install duckduckgo-search


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return the top results.

    Parameters
    ----------
    query       : search query string
    max_results : how many results to return (default 5, max 10)
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return "ERROR: duckduckgo-search not installed. Run: pip install duckduckgo-search"

    max_results = min(max_results, 10)
    results = []

    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(f"[{r['title']}]\n{r['href']}\n{r['body']}")

    if not results:
        return f"No results found for: {query}"

    return f"Search results for '{query}':\n\n" + "\n\n---\n\n".join(results)


@tool
def web_fetch(url: str) -> str:
    """Fetch the text content of a web page.

    Parameters
    ----------
    url : full URL to fetch (must start with http:// or https://)
    """
    try:
        import httpx
    except ImportError:
        return "ERROR: httpx not installed. Run: pip install httpx"

    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}' — must start with http:// or https://"

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 (Beaver Agent)"})
            resp.raise_for_status()
            # Strip to plain text — very basic, no HTML parser dependency
            text = resp.text
            # Truncate to avoid flooding context
            if len(text) > 6000:
                text = text[:6000] + "\n\n[... truncated ...]"
            return text
    except Exception as exc:
        return f"ERROR fetching {url}: {exc}"


TOOLS = [web_search, web_fetch]
