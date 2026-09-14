"""
Wikipedia, over the official API.

This used to call the `wikipedia` pip package, which scrapes the rendered site.
It returned "Expecting value: line 1 column 1 (char 0)" for every query — the
JSON parser being handed HTML, which is what that message always means. The
tool then passed the error string back as if it were a result, so Diablo
cheerfully read a stack-trace fragment out loud as an answer.

The REST summary endpoint is public, unauthenticated, returns real JSON, and
needs nothing but urllib.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
SEARCH = "https://en.wikipedia.org/w/api.php"
AGENT = "Diablo/2.0 (local voice assistant; https://github.com/)"
TIMEOUT = int(os.getenv("DIABLO_WEB_TIMEOUT", "10"))
# What to wait when Wikipedia says 429 but does not say for how long,
# and the longest wait worth taking before it is faster to fail and say so.
RETRY_AFTER = 2.0
MAX_RETRY_WAIT = 12.0


def _get(url: str, params: dict = None) -> dict:
    """
    One request, with Wikipedia's own rate limit obeyed rather than reported.

    api.php returns 429 with a Retry-After header under load, and the tool used
    to hand that straight back as "I could not reach Wikipedia, Sir." — which
    is both wrong and useless, because Wikipedia was reachable and asking again
    in nine seconds would have worked. It waits once, and only for as long as
    the server asked for.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={
        "User-Agent": AGENT, "Accept": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            try:
                wait = float(e.headers.get("Retry-After") or RETRY_AFTER)
            except (TypeError, ValueError):
                wait = RETRY_AFTER
            time.sleep(min(max(wait, 0.0), MAX_RETRY_WAIT))
    raise urllib.error.URLError("unreachable")


def _best_title(query: str) -> str:
    """
    The article the search endpoint thinks he meant.

    The summary endpoint is tried first, because it is the one that is not
    rate-limited and most queries arrive already spelled like a title —
    "Treaty of Versailles" resolves in one request and never touches the search
    API at all. Search is the fallback, and it is what makes "the versailles
    treaty" land on the same page.
    """
    direct = query.strip().replace(" ", "_")
    try:
        data = _get(API + urllib.parse.quote(direct, safe=""))
        title = (data.get("titles", {}) or {}).get("canonical") or data.get("title")
        if title and not data.get("type", "").endswith("disambiguation"):
            return title.replace("_", " ")
    except Exception:
        pass
    data = _get(SEARCH, {"action": "query", "list": "search", "srsearch": query,
                         "srlimit": 1, "format": "json", "utf8": 1})
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else query


def summary_with_title(query: str, sentences: int = 3):
    """
    (title, summary). The title matters when this is used as a fallback.

    Wikipedia's search will happily answer "who won the 2026 Monaco Grand
    Prix" with the article on the 2023 race, and a summary quoted without its
    title reads as an answer to the question that was asked. Naming the
    article makes the mismatch visible instead of hiding it.
    """
    query = (query or "").strip()
    if not query:
        return "", "I need something to look up, Sir."
    try:
        title = _best_title(query)
    except Exception as e:
        return "", f"I could not reach Wikipedia, Sir. ({type(e).__name__})"
    return title, _summarise(title, query, sentences)


def search_wikipedia(query: str, sentences: int = 3) -> str:
    """A short summary of the best-matching article, or an honest failure."""
    query = (query or "").strip()
    if not query:
        return "I need something to look up, Sir."
    try:
        title = _best_title(query)
    except Exception as e:
        return f"I could not reach Wikipedia, Sir. ({type(e).__name__})"
    return _summarise(title, query, sentences)


def _summarise(title: str, query: str, sentences: int) -> str:
    try:
        data = _get(API + urllib.parse.quote(title.replace(" ", "_"), safe=""))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"Wikipedia has no article for {query}, Sir."
        return f"Wikipedia returned {e.code}, Sir."
    except Exception as e:
        # Named plainly. The old version put the raw exception in the spoken
        # answer, which is how "Expecting value: line 1 column 1" got read out.
        return f"I could not reach Wikipedia, Sir. ({type(e).__name__})"

    if data.get("type", "").endswith("disambiguation"):
        return (f"{title} could mean several things, Sir. "
                "Tell me which one and I will look again.")

    extract = (data.get("extract") or "").strip()
    if not extract:
        return f"Wikipedia has an article on {title} but no summary, Sir."

    parts = [p.strip() for p in extract.replace("\n", " ").split(". ") if p.strip()]
    summary = ". ".join(parts[:max(1, sentences)])
    if not summary.endswith("."):
        summary += "."
    return summary


if __name__ == "__main__":
    import sys
    print(search_wikipedia(" ".join(sys.argv[1:]) or "Treaty of Versailles"))
