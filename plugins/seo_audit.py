"""
Beaver Plugin — SEO Audit
===========================

Two tools that seo.md's own checklist calls for (robots.txt/sitemap
validation, structured data) but had no real implementation — the model
previously had to reconstruct these by eye from raw http_get text, which
is exactly the kind of unreliable free-form parsing that leads to missed
or fabricated findings.

Useful for:
  seo        — crawlability checks, structured data validation

Tools
-----
  check_robots_sitemap  — fetch and parse robots.txt + validate the
                            referenced sitemap.xml is reachable and valid XML
  check_structured_data — fetch a page and extract/validate any JSON-LD
                            structured data blocks
"""

import json
import re
import httpx
from langchain_core.tools import tool

PERSONA  = ["seo"]
ENABLED  = True

_UA              = "Beaver-Agent/3.0 (seo-audit)"
_DEFAULT_TIMEOUT = 15.0


def _base_url(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url.rstrip("/")


@tool
def check_robots_sitemap(url: str, timeout: float = 15.0) -> str:
    """Fetch and audit robots.txt, and validate any referenced sitemap(s).

    Checks: robots.txt reachability, disallow rules, sitemap directive
    presence, and whether the referenced sitemap.xml is itself reachable
    and parses as valid XML.

    Parameters
    ----------
    url     : any URL on the site to audit, e.g. "https://example.com" or
              "https://example.com/some/page" — only the domain is used
    timeout : per-request timeout in seconds (default 15)
    """
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"

    base = _base_url(url)
    robots_url = f"{base}/robots.txt"
    timeout = min(max(5.0, timeout), 60.0)

    lines = [f"Auditing: {base}\n"]

    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True) as client:
            robots_resp = client.get(robots_url)
    except httpx.TimeoutException:
        return f"[check_robots_sitemap] Timeout fetching {robots_url}"
    except Exception as exc:
        return f"[check_robots_sitemap] Error fetching robots.txt: {exc}"

    if robots_resp.status_code != 200:
        lines.append(f"robots.txt: NOT FOUND (HTTP {robots_resp.status_code}) — site allows all crawling by default absence of the file.")
        sitemap_urls = []
    else:
        body = robots_resp.text
        lines.append(f"robots.txt: OK (HTTP 200, {len(body)} bytes)\n")

        disallow_lines = [l.strip() for l in body.splitlines() if l.strip().lower().startswith("disallow:")]
        lines.append(f"Disallow rules ({len(disallow_lines)}):")
        lines.extend(f"  {d}" for d in disallow_lines[:30])
        if len(disallow_lines) > 30:
            lines.append(f"  ... and {len(disallow_lines) - 30} more")

        sitemap_lines = [l.strip() for l in body.splitlines() if l.strip().lower().startswith("sitemap:")]
        sitemap_urls = [l.split(":", 1)[1].strip() for l in sitemap_lines if ":" in l]
        lines.append(f"\nSitemap directive(s) in robots.txt: {len(sitemap_urls)}")
        lines.extend(f"  {s}" for s in sitemap_urls)

    if not sitemap_urls:
        # fall back to the conventional default location
        sitemap_urls = [f"{base}/sitemap.xml"]
        lines.append(f"\nNo sitemap directive found — checking conventional default: {sitemap_urls[0]}")

    lines.append("\nSitemap validation:")
    for sm_url in sitemap_urls[:5]:
        try:
            with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True) as client:
                sm_resp = client.get(sm_url)
        except httpx.TimeoutException:
            lines.append(f"  {sm_url}: TIMEOUT")
            continue
        except Exception as exc:
            lines.append(f"  {sm_url}: ERROR — {exc}")
            continue

        if sm_resp.status_code != 200:
            lines.append(f"  {sm_url}: HTTP {sm_resp.status_code} — NOT REACHABLE")
            continue

        content = sm_resp.text
        is_xml = content.strip().startswith("<?xml") or "<urlset" in content or "<sitemapindex" in content
        url_count = content.count("<loc>")
        if is_xml:
            lines.append(f"  {sm_url}: OK, valid XML, {url_count} <loc> entries")
        else:
            lines.append(f"  {sm_url}: HTTP 200 but does NOT look like valid sitemap XML — check content manually")

    return "\n".join(lines)


@tool
def check_structured_data(url: str, timeout: float = 15.0) -> str:
    """Fetch a page and extract/validate JSON-LD structured data blocks.

    Finds all <script type="application/ld+json"> blocks, parses each as
    JSON, and reports the schema.org @type found plus any parse errors —
    a malformed JSON-LD block silently fails in search engines with no
    visible error, so catching this is the main value here.

    Parameters
    ----------
    url     : the page URL to check
    timeout : request timeout in seconds (default 15)
    """
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"

    timeout = min(max(5.0, timeout), 60.0)
    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True) as client:
            resp = client.get(url)
    except httpx.TimeoutException:
        return f"[check_structured_data] Timeout fetching {url}"
    except Exception as exc:
        return f"[check_structured_data] Error: {exc}"

    if resp.status_code != 200:
        return f"[check_structured_data] HTTP {resp.status_code} — could not fetch page."

    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        resp.text, re.DOTALL | re.IGNORECASE,
    )

    if not blocks:
        return f"[check_structured_data] No JSON-LD structured data found on {url}."

    lines = [f"Found {len(blocks)} JSON-LD block(s) on {url}:\n"]
    for i, block in enumerate(blocks, 1):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError as exc:
            lines.append(f"  Block {i}: INVALID JSON — {exc}")
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                lines.append(f"  Block {i}: unexpected structure (not an object)")
                continue
            schema_type = item.get("@type", "UNKNOWN")
            has_context = "@context" in item
            lines.append(
                f"  Block {i}: @type={schema_type}  @context={'present' if has_context else 'MISSING'}"
            )

    return "\n".join(lines)


TOOLS = [check_robots_sitemap, check_structured_data]
