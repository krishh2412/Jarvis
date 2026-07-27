"""Web search, page reading and URL opening."""

from __future__ import annotations

import webbrowser

from jarvis.tools.registry import tool

MAX_PAGE_CHARS = 12000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _search_duckduckgo_html(query: str, max_results: int) -> list[dict]:
    """Search via DuckDuckGo's HTML endpoint using httpx.

    The ddgs library bundles a Rust HTTP client (primp) that does its own DNS
    resolution and fails outright in some environments ("DNS Query Refused")
    even when the machine's own resolver is fine. httpx uses the system stack
    and just works, so this is the primary path and ddgs is the fallback.
    """
    import html as html_lib
    import re

    import httpx

    resp = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": UA},
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()

    from urllib.parse import unquote

    from lxml import html as lxml_html

    tree = lxml_html.fromstring(resp.text)
    results: list[dict] = []
    # xpath rather than cssselect so there is no dependency on the cssselect
    # package, which is not otherwise pulled in.
    for link in tree.xpath('//a[contains(@class, "result__a")]'):
        href = link.get("href", "")
        # DDG wraps outbound links in a redirect; unwrap to the real target.
        match = re.search(r"uddg=([^&]+)", href)
        if match:
            href = unquote(match.group(1))
        if not href.startswith("http"):
            continue
        title = link.text_content().strip()
        # The snippet is a sibling anchor within the same result block.
        snippet = ""
        block = link.xpath('./ancestor::div[contains(@class, "result")][1]')
        if block:
            snip = block[0].xpath('.//a[contains(@class, "result__snippet")]')
            if snip:
                snippet = snip[0].text_content().strip()
        results.append({
            "title": html_lib.unescape(title),
            "url": href,
            "snippet": html_lib.unescape(snippet)[:400],
        })
        if len(results) >= max_results:
            break
    return results


@tool(category="web")
def web_search(query: str, max_results: int = 6) -> dict:
    """Search the web and return titles, URLs and snippets.

    Use this for anything you do not reliably know: current events, prices,
    release dates, populations, documentation, local business details.

    Args:
        query: The search query.
        max_results: How many results to return.
    """
    # The ddgs library used to sit behind this as a fallback. It was removed:
    # its bundled Rust HTTP client cannot resolve anything on this machine
    # (verified again 2026-07-27 — "ConnectError: error sending request"), so it
    # never once produced a result, and on every failed search it burned seconds
    # trying four dead backends in turn before giving up. A fallback that cannot
    # succeed is worse than none: it only adds latency to the error path.
    try:
        results = _search_duckduckgo_html(query, max_results)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"search failed: {exc}"}

    if results:
        return {"query": query, "results": results}
    return {"query": query, "results": [],
            "note": "no results found; try rephrasing the query"}


@tool(category="web")
def read_webpage(url: str, max_chars: int = MAX_PAGE_CHARS) -> dict:
    """Fetch a web page and extract its readable text.

    Strips navigation, ads and boilerplate, returning article content only.

    Args:
        url: The page to read.
        max_chars: Truncate the extracted text beyond this length.
    """
    import httpx
    import trafilatura

    try:
        response = httpx.get(
            url, timeout=20, follow_redirects=True, headers={"User-Agent": UA}
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not fetch {url}: {exc}"}

    text = trafilatura.extract(
        response.text, include_comments=False, include_tables=True
    )
    if not text:
        return {"error": "no readable content extracted from the page"}

    return {
        "url": str(response.url),
        "content": text[:max_chars],
        "truncated": len(text) > max_chars,
        "total_chars": len(text),
    }


@tool(destructive=True, category="web")
def open_url(url: str) -> dict:
    """Open a URL in the default browser.

    Args:
        url: The address to open. A bare domain is upgraded to https.
    """
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url
    webbrowser.open(url)
    return {"opened": url}


# Spoken names that are websites, not installed programs. This is the fix for
# "open YouTube" being sent to the app launcher and failing -- these live on
# the web and open in the browser.
KNOWN_SITES: dict[str, str] = {
    "youtube": "https://youtube.com",
    "google": "https://google.com",
    "gmail": "https://mail.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://reddit.com",
    "github": "https://github.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "netflix": "https://netflix.com",
    "amazon": "https://amazon.com",
    "wikipedia": "https://wikipedia.org",
    "whatsapp": "https://web.whatsapp.com",
    "facebook": "https://facebook.com",
    "instagram": "https://instagram.com",
    "linkedin": "https://linkedin.com",
    "twitch": "https://twitch.tv",
    "spotify web": "https://open.spotify.com",
    "outlook": "https://outlook.live.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
}


@tool(destructive=True, category="web")
def open_website(name_or_url: str) -> dict:
    """Open a website in the browser by common name or address.

    This is how you open anything that lives on the web -- YouTube, Gmail,
    Reddit, a specific URL. These are NOT applications on the machine, so do
    not try to launch them as programs; open them here. "Open YouTube" means
    open youtube.com in the browser.

    Args:
        name_or_url: A well-known site name ("youtube", "gmail"), a bare domain
            ("example.com"), or a full URL.
    """
    key = name_or_url.strip().lower()
    if key in KNOWN_SITES:
        target = KNOWN_SITES[key]
    elif "." in key or key.startswith(("http://", "https://")):
        target = key if key.startswith("http") else "https://" + key
    else:
        # Not a known site and not domain-shaped: treat it as a search rather
        # than guessing at a URL that may not exist.
        target = f"https://www.google.com/search?q={name_or_url.replace(' ', '+')}"
    webbrowser.open(target)
    return {"opened": target}


@tool(category="web")
def search_and_read(query: str, results_to_read: int = 2) -> dict:
    """Search the web then read the top results in one step.

    Cheaper than issuing a search followed by separate page reads, and the
    usual way to answer a factual question that needs current information.

    Args:
        query: The search query.
        results_to_read: How many of the top results to fetch and extract.
    """
    search = web_search(query, max_results=max(results_to_read, 4))
    if "error" in search:
        return search

    pages = []
    for hit in search["results"][:results_to_read]:
        page = read_webpage(hit["url"], max_chars=6000)
        if "error" in page:
            continue
        pages.append(
            {"title": hit["title"], "url": hit["url"], "content": page["content"]}
        )

    return {
        "query": query,
        "pages": pages,
        "other_results": [
            {"title": h["title"], "url": h["url"]}
            for h in search["results"][results_to_read:]
        ],
    }
