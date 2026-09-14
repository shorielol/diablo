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
CACHE_SECONDS = float(os.getenv("DIABLO_SEARCH_CACHE", "300"))


class Throttled(Exception):
    """The engine is refusing to answer, which is not the same as no results."""


def _blocked(page: str) -> bool:
    lowered = page.lower()
    return ("anomaly" in lowered or "unusual traffic" in lowered
            or "captcha" in lowered) and "result__a" not in page


def search_web(query: str, limit: int = MAX_RESULTS) -> List[dict]:
    """
    Real results, as a list of {title, url, snippet}.

    Raises Throttled when the engine is refusing. Returns [] only when there
    genuinely are no results. Callers must tell those two apart out loud.
    """
    query = (query or "").strip()
    if not query:
        return []

    cached = _cache.get(query)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1][:max(1, limit)]

    # Self-throttle. Most of the blocking this ran into was self-inflicted:
    # several searches fired back to back inside one agent turn.
    gap = time.time() - _last_call[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_call[0] = time.time()

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
    out = [r for r in parser.results if r["title"] and r["url"]]
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
