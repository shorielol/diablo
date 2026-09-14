import re
import subprocess
import time
import webbrowser
from typing import List, Optional, Tuple
from skills import browsers  # noqa: E402

def run_applescript(script: str) -> str:
    """Runs an AppleScript and returns the output."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {str(e)}"

# What a request for "music" with no song in it actually means.
#
# "Play music" used to be handed to the track search as the literal query
# "music", which found nothing in the library, fell through to the web, and
# opened a random YouTube video. Meanwhile Apple Music was open with 53 tracks
# in it. Nobody asking for music wants a search for a song called Music.
# Words that carry no information about *which* music.
FILLER = {
    "play", "put", "on", "start", "some", "please", "for", "me", "my", "the",
    "a", "an", "of", "up", "just", "can", "you", "could", "would", "i", "want",
    "would", "like", "to", "hear", "listen", "listening", "lets", "let", "us",
    "something", "anything", "stuff", "now", "go", "ahead", "give",
}

# What is left when the filler is gone, for a request that names no music.
GENERIC = {
    "", "music", "song", "songs", "tune", "tunes", "track", "tracks",
    "library", "playlist", "playlists", "shuffle", "album", "albums",
}


def wants_shuffle(query: str) -> bool:
    """
    True when he asked for music rather than for a particular piece of it.

    Tokenised, not substring-replaced. The first version stripped the filler
    word "on" from anywhere in the string, which turned "songs" into "s gs"
    and quietly stopped "play songs" from being recognised at all.
    """
    words = re.findall(r"[a-z']+", (query or "").lower())
    remaining = [w for w in words if w not in FILLER]
    if not remaining:
        return True
    if len(remaining) > 2:
        return False
    return all(w in GENERIC for w in remaining)


def shuffle_library() -> str:
    """Shuffle everything in Apple Music and start playing."""
    script = '''
    tell application "Music"
        if (count of tracks of library playlist 1) is 0 then
            return "EMPTY"
        end if
        set shuffle enabled to true
        play library playlist 1
        delay 0.6
        try
            return (name of current track) & " by " & (artist of current track)
        on error
            return "PLAYING"
        end try
    end tell
    '''
    result = run_applescript(script)
    if result == "EMPTY":
        return ""
    if result.startswith("Error:"):
        return ""
    return result


# ─── The library, matched properly ───────────────────────────────────────────
#
# The old search asked AppleScript for `every track whose name contains "<the
# whole query>" or artist contains "<the whole query>"`. That works only if
# you say the title and nothing else. Measured against this library:
#
#     "The Truth"                 1 match
#     "the truth by lil tecca"    0 matches
#     "ivy by frank ocean"        0 matches
#
# Both of those songs are in the library. Phrase it the way a person speaks
# and the search misses, the app types the query into Music's search box with
# synthetic keystrokes, presses Tab and Return at whatever has focus, and then
# gives up and opens YouTube. That is the "it searches the song but never
# plays it" — it was barely searching the library at all.
#
# Fifty tracks is nothing. Read them once and match in Python, where there can
# be a real scorer rather than a substring test.

_library_cache: Tuple[float, List[dict]] = (0.0, [])
LIBRARY_TTL = 300.0

# Words that describe the request rather than the music.
_SKIP = {"play", "put", "on", "some", "the", "a", "an", "by", "from", "for",
         "me", "please", "song", "track", "tune", "listen", "to", "of", "my"}


def library(force: bool = False) -> List[dict]:
    """Every track, as {id, name, artist}. Cached for a few minutes."""
    global _library_cache
    age, cached = _library_cache
    if cached and not force and (time.time() - age) < LIBRARY_TTL:
        return cached
    script = """
    tell application "Music"
        set out to ""
        repeat with t in (get every track of library playlist 1)
            set out to out & (persistent ID of t) & tab & (name of t) & tab & (artist of t) & linefeed
        end repeat
        return out
    end tell
    """
    raw = run_applescript(script)
    tracks = []
    for line in (raw or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].strip():
            tracks.append({"id": parts[0].strip(), "name": parts[1].strip(),
                           "artist": parts[2].strip()})
    if tracks:
        _library_cache = (time.time(), tracks)
    return tracks


def _tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
            if w and w not in _SKIP]


def find_in_library(query: str):
    """
    The track he meant, or None.

    Scored on how much of what he said the title and artist account for, and
    how much of the title he actually said. Both halves matter: the first
    alone matches any song by the right artist, the second alone matches "The
    Truth" for someone asking about truth or dare.

    None is a real answer. A wrong song is worse than falling through to the
    web, because the web at least gets the right one.
    """
    wanted = _tokens(query)
    if not wanted:
        return None
    best, best_score = None, 0.0
    for track in library():
        title = set(_tokens(track["name"]))
        artist = set(_tokens(track["artist"]))
        if not title:
            continue
        explained = sum(1 for w in wanted if w in title or w in artist)
        coverage = explained / len(wanted)
        title_hit = sum(1 for w in title if w in wanted) / len(title)
        score = coverage * 0.6 + title_hit * 0.4
        if score > best_score:
            best, best_score = track, score
    return best if best_score >= 0.72 else None


def play_track(track: dict) -> str:
    """
    Play one track by its persistent ID. Returns what is actually playing.

    It waits for the persistent ID of the current track to become the one that
    was asked for, rather than reading the name after a fixed delay. Music
    takes a moment to change over, and a fixed delay caught the *previous*
    track: asked for Irish Goodbye it played Irish Goodbye and announced The
    Truth, which is the kind of small lie that makes you stop trusting it.
    """
    script = f"""
    tell application "Music"
        set shuffle enabled to false
        set t to (first track of library playlist 1 whose persistent ID is "{track['id']}")
        play t
        repeat 25 times
            try
                if (persistent ID of current track) is "{track['id']}" then
                    return (name of current track) & " by " & (artist of current track)
                end if
            end try
            delay 0.08
        end repeat
        return "TIMEOUT"
    end tell
    """
    result = run_applescript(script)
    if result.startswith("Error:") or result == "TIMEOUT":
        return ""
    return result


def play_pause():
    return run_applescript('tell application "Music" to playpause')

def next_track():
    return run_applescript('tell application "Music" to next track')

def previous_track():
    return run_applescript('tell application "Music" to previous track')

def resolve_track(query: str, service: str = "youtube") -> Optional[str]:
    """
    Turn "play bohemian rhapsody" into a URL that plays bohemian rhapsody.

    This is the whole fix. The old path opened a search page, slept four
    seconds, and sent Return twice, hoping focus had landed on a result — so
    "play a specific song" reliably produced a page of search results and the
    occasional unrelated track. A watch URL autoplays, and there is nothing
    left to guess at.
    """
    import re
    import urllib.parse
    import urllib.request

    # YouTube's own results page, not a search engine. Going through
    # DuckDuckGo worked for about six queries and then returned HTTP 202 with
    # an "anomaly" page; Mojeek serves a JavaScript captcha. YouTube answers
    # plainly, has the official video as the first hit, and needs no key.
    url = ("https://www.youtube.com/results?search_query="
           + urllib.parse.quote(query))
    request = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
                       "Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            page = response.read().decode("utf-8", "replace")
    except Exception:
        return None
    ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', page)
    if not ids:
        return None
    first = ids[0]
    if service == "youtube_music":
        return f"https://music.youtube.com/watch?v={first}"
    return f"https://www.youtube.com/watch?v={first}"


def play_on_web_service(query: str, service: str = "youtube"):
    """Resolve the track, then open it. Falls back to a search page."""
    if service == "spotify":
        url = f"https://open.spotify.com/search/{query.replace(' ', '+')}"
        print(f"  [Music] Spotify search: {url}")
        browsers.open_url(url)
        return "opened on spotify"

    url = resolve_track(query)
    if url:
        print(f"  [Music] resolved to {url}")
        browsers.open_url(url)
        return f"playing {query} on YouTube"

    fallback = f"https://music.youtube.com/search?q={query.replace(' ', '+')}"
    print(f"  [Music] could not resolve a track; opening {fallback}")
    browsers.open_url(fallback)
    return f"could not find a single track for {query}; opened a search instead"


def search_and_play(query: str):
    """
    Play a named song: from the library if it is there, from the web if not.

    The middle step is gone. It used to activate Music, type the query into
    the search box with synthetic keystrokes, wait three and a half seconds,
    then press Tab and Return at whatever happened to have focus. Guessing at
    focus is not searching, and it is what you were watching when the song got
    searched for and never played.

    Either the track is in the library, in which case it plays by persistent
    ID with nothing guessed at all, or it is not, in which case say so plainly
    and stream it.
    """
    query = (query or "").strip()
    if not query:
        return ""

    hit = find_in_library(query)
    if hit:
        now = play_track(hit)
        if now:
            print(f"  [Music] from your library: {now}")
            return f"Playing {now} from your library."
        # Found and would not play — a cloud track with no local file, usually.
        # Falling through is right; pretending it worked is not.
        print(f"  [Music] {hit['name']} is in the library but would not play")

    print(f"  [Music] '{query}' is not in your library; streaming instead")
    url = resolve_track(query)
    if url:
        browsers.open_url(url)
        return ("That one is not in your Apple Music library, Sir, "
                "so I have put it on YouTube.")

    fallback = f"https://music.youtube.com/search?q={query.replace(' ', '+')}"
    browsers.open_url(fallback)
    return f"I could not find a single track for {query}, so here is a search."
