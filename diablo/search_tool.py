"""
Web search that brings words back.

`google_search` below opens a browser and returns "the results are on screen
for the user to read; I cannot see them myself." That is a bookmark, not
research — and "research this for me" was one of the things Diablo was
supposed to do. It is kept, because sometimes showing him the page really is
the right answer, but it is no longer the only option.

`search_web` reads. DuckDuckGo's HTML endpoint needs no key and no account,
which matters for something that is meant to work on a laptop with nothing
configured. Parsed with the stdlib HTML parser rather than a scraping library,
for the same reason the rest of this project has no build step.
"""

import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from typing import List, Optional

DDG = "https://html.duckduckgo.com/html/"
AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = int(os.getenv("DIABLO_WEB_TIMEOUT", "12"))
MAX_RESULTS = int(os.getenv("DIABLO_SEARCH_RESULTS", "5"))


class _Results(HTMLParser):
    """Pulls titles, links and snippets out of DuckDuckGo's HTML results."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: List[dict] = []
        self._grabbing: Optional[str] = None
        self._buffer: List[str] = []
        self._href = ""
        # Which tag opened the capture, and how deep we are inside it.
        # DuckDuckGo bolds the query terms inside every snippet, so a parser
        # that stops at the first closing tag keeps the one word before the
        # first <b> — which is how every snippet came back as "The" or "2026".
        self._tag: Optional[str] = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if self._grabbing:
            if tag == self._tag:
                self._depth += 1
            return
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._grabbing, self._buffer = "title", []
            self._tag, self._depth = tag, 1
            self._href = attrs.get("href", "")
        elif "result__snippet" in classes:
            self._grabbing, self._buffer = "snippet", []
            self._tag, self._depth = tag, 1

    def handle_data(self, data):
        if self._grabbing:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if not self._grabbing:
            return
        if tag == self._tag:
            self._depth -= 1
            if self._depth > 0:
                return
        else:
            return
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if self._grabbing == "title" and text:
            self.results.append({"title": text, "url": _real_url(self._href),
                                 "snippet": ""})
        elif self._grabbing == "snippet" and text and self.results:
            if not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = text
        self._grabbing, self._buffer, self._tag, self._depth = None, [], None, 0


def _real_url(href: str) -> str:
    """DuckDuckGo wraps every link in a redirect; unwrap it."""
    if not href:
        return ""
    if "uddg=" in href:
        try:
            return urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        except Exception:
            return href
    return href if href.startswith("http") else "https:" + href


# Being throttled, and admitting it.
#
# DuckDuckGo answers about half a dozen queries in quick succession and then
# returns HTTP 202 carrying an "anomaly" page — no error, no status code worth
# reading, just a page with no results in it. Parsed naively that is
# indistinguishable from "the web has nothing on this", and Diablo would say
# so out loud. It is a very different sentence from "I have been blocked, try
# again in a minute", and only one of them is true.
_last_call = [0.0]
_cache: dict = {}
MIN_GAP = float(os.getenv("DIABLO_SEARCH_GAP", "1.2"))
# Two attempts per provider, then the next provider. Short, because a voice
# interface cannot spend a minute backing off politely.
RETRIES = int(os.getenv("DIABLO_SEARCH_RETRIES", "2"))
BACKOFF = float(os.getenv("DIABLO_SEARCH_BACKOFF", "0.8"))
# Last known state of each provider, for `health()`. Not persisted: it is a
# diagnostic for this session, not a record.
_health: dict = {}


def health() -> dict:
    """What each search provider did last time it was asked."""
    return dict(_health)
CACHE_SECONDS = float(os.getenv("DIABLO_SEARCH_CACHE", "300"))


class Throttled(Exception):
    """The engine is refusing to answer, which is not the same as no results."""


def _blocked(page: str) -> bool:
    lowered = page.lower()
    return ("anomaly" in lowered or "unusual traffic" in lowered
            or "captcha" in lowered) and "result__a" not in page


def _via_ddgs(query: str, limit: int) -> List[dict]:
    """
    Search through the ddgs package, or raise.

    The package is the maintained continuation of duckduckgo-search, which
    stopped at 8.1.1 and was renamed at version 9 when it became a metasearch
    library — it aggregates several engines rather than only DuckDuckGo, which
    is most of why it is harder to rate-limit than scraping one HTML endpoint.

    It returns {title, href, body}. That gets mapped to {title, url, snippet}
    here rather than anywhere else, because three callers and every test in the
    suite already read those names, and a search result is not worth a breaking
    change across the codebase.
    """
    from ddgs import DDGS
    from ddgs.exceptions import RatelimitException, TimeoutException

    try:
        with DDGS() as engine:
            rows = engine.text(query, max_results=max(1, limit))
    except RatelimitException as e:
        raise Throttled(f"the search engine is rate-limiting this machine ({e})")
    except TimeoutException as e:
        raise Throttled(f"the search engine timed out ({e})")

    out = []
    for row in rows or []:
        title = (row.get("title") or "").strip()
        url = (row.get("href") or "").strip()
        if not title or not url:
            continue
        out.append({"title": title, "url": url,
                    "snippet": (row.get("body") or "").strip(),
                    "provider": "ddgs"})
    return out


def _via_scraping(query: str, limit: int) -> List[dict]:
    """The old HTML-endpoint scrape, kept as the fallback it now is."""
    body = urllib.parse.urlencode({"q": query}).encode()
    request = urllib.request.Request(DDG, data=body, headers={
        "User-Agent": AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            page = response.read().decode("utf-8", "replace")
    except Exception:
        return []

    if _blocked(page):
        raise Throttled("the search engine is rate-limiting this machine")

    parser = _Results()
    try:
        parser.feed(page)
    except Exception:
        pass
    return [dict(r, provider="ddg-html")
            for r in parser.results if r["title"] and r["url"]]


def search_web(query: str, limit: int = MAX_RESULTS) -> List[dict]:
    """
    Real results, as a list of {title, url, snippet}.

    Raises Throttled when the engine is refusing. Returns [] only when there
    genuinely are no results. Callers must tell those two apart out loud.

    Two ways of getting them, in order. The ddgs package queries the engines'
    own JSON endpoints across several backends; the scrape reads one HTML page
    and gets an "anomaly" page after about half a dozen queries in a row. The
    scrape stays as the fallback because it needs nothing installed, and a
    search tool whose only path is a dependency is a search tool that breaks on
    an upgrade.
    """
    query = (query or "").strip()
    if not query:
        return []

    cached = _cache.get(query)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1][:max(1, limit)]

    # Self-throttle, kept. Most of the blocking this ran into was self-
    # inflicted — several searches fired back to back inside one agent turn —
    # and that is just as true through a package as through a scrape. Asking
    # politely is what keeps the endpoint answering.
    gap = time.time() - _last_call[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_call[0] = time.time()

    out: List[dict] = []
    throttled: Optional[Throttled] = None

    # Providers in order, each tried with a short backoff before the next.
    #
    # ddgs reduces throttling; it does not abolish it. Ten consecutive searches
    # succeeding is not immunity, and the honest design is to assume every
    # provider will refuse eventually and to say so when they all do rather
    # than reporting no results. Backoff is deliberately short: this is a voice
    # interface and a minute of silence is worse than an honest failure.
    for name, provider in (("ddgs", _via_ddgs), ("ddg-html", _via_scraping)):
        for attempt in range(RETRIES):
            try:
                out = provider(query, limit)
                if out:
                    _health[name] = {"ok": True, "at": time.time()}
                    break
            except Throttled as e:
                throttled = e
                _health[name] = {"ok": False, "at": time.time(),
                                 "why": "throttled"}
                if attempt + 1 < RETRIES:
                    time.sleep(BACKOFF * (2 ** attempt))
                    continue
            except ImportError:
                _health[name] = {"ok": False, "at": time.time(),
                                 "why": "not installed"}
            except Exception as e:
                _health[name] = {"ok": False, "at": time.time(),
                                 "why": type(e).__name__}
                print(f"  [Search] {name} failed "
                      f"({type(e).__name__}: {e}); trying the next provider")
            break
        if out:
            break

    # Both refused, and nothing came back. Say that, rather than "no results" —
    # they are different sentences and only one of them is true.
    if not out and throttled is not None:
        raise throttled

    if out:
        _cache[query] = (time.time(), out)
    return out[:max(1, limit)]


def summarise(query: str, limit: int = 3) -> str:
    """Search results as something that can be read out loud."""
    results = search_web(query, limit=limit)
    if not results:
        return ""
    lines = []
    for r in results:
        snippet = html.unescape(r["snippet"])[:300]
        where = urllib.parse.urlparse(r["url"]).netloc.replace("www.", "")
        lines.append(f"{r['title']} ({where}): {snippet}" if snippet
                     else f"{r['title']} ({where})")
    return "\n\n".join(lines)


def google_search(query: str):
    """
    Opens the default web browser with Google Search results.
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.google.com/search?q={encoded_query}"
    webbrowser.open(url)
    return f"Searching for {query} on Google, Sir."

def find_news(query: str):
    """
    Searches specifically in Google News.
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.google.com/search?q={encoded_query}&tbm=nws"
    webbrowser.open(url)
    return f"Finding latest articles on {query}, Sir."

def play_latest_video(channel_query: str):
    """
    Finds the latest video from a channel using YouTube filters.
    sp=CAI%253D is the filter for 'Upload Date'
    """
    encoded_query = urllib.parse.quote(channel_query)
    # This URL searches and applies the 'Upload date' filter automatically
    url = f"https://www.youtube.com/results?search_query={encoded_query}&sp=CAI%253D"
    webbrowser.open(url)
    return f"Finding the latest video for {channel_query}, Sir."

def search_youtube(query: str):
    """
    Opens YouTube search results for a specific query.
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.youtube.com/results?search_query={encoded_query}"
    webbrowser.open(url)
    return f"Searching YouTube for {query}, Sir."

if __name__ == "__main__":
    # Test
    play_latest_video("Wembu")


# ─── Reading a page ──────────────────────────────────────────────────────────

class _Readable(HTMLParser):
    """Visible text only: no script, no style, no navigation furniture."""

    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "svg",
            "form", "aside", "button", "select"}
    BREAK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6",
             "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._muted = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._muted += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._muted:
            self._muted -= 1

    def handle_data(self, data):
        if not self._muted:
            text = data.strip()
            if text:
                self.parts.append(text + " ")


def read_page(url: str, limit: int = 6000) -> str:
    """
    The readable text of a page, or "".

    Needed because a search snippet is two lines and instructions are ten. It
    is deliberately crude — no JavaScript, no rendering — which means it works
    on documentation and fails on single-page applications, and that is the
    right trade for something that has to answer in a couple of seconds.

    Whatever comes back is somebody else's words off the open web. It is data,
    never an instruction, and every caller has to treat it that way.
    """
    if not url or not url.startswith("http"):
        return ""
    request = urllib.request.Request(url, headers={
        "User-Agent": AGENT, "Accept": "text/html", "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            kind = (response.headers.get("Content-Type") or "").lower()
            if "html" not in kind and "text" not in kind:
                return ""
            raw = response.read(2_000_000).decode("utf-8", "replace")
    except Exception:
        return ""
    parser = _Readable()
    try:
        parser.feed(raw)
    except Exception:
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:limit]
