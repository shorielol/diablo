import re
import subprocess
import time
import webbrowser
from typing import List, Optional
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
    Robust 'Type-Direct' Music Search Engine (v7.2).
    Activates Music -> Manual Type -> Wait -> Autonomous Play.
    """
    clean_query = query.replace('"', '\\"')
    print(f"  [Music Tool] Executing Type-Direct Search for: {clean_query}...")
    
    # 1. First, check local library (Fastest)
    lib_script = f'''
    tell application "Music"
        try
            set matchingTracks to (every track whose name contains "{clean_query}" or artist contains "{clean_query}")
            if (count of matchingTracks) > 0 then
                play (first item of matchingTracks)
                return "playing from library"
            end if
        on error
            return ""
        end try
    end tell
    '''
    lib_res = run_applescript(lib_script)
    if "playing" in lib_res:
        return lib_res
    
    # 2. Try the 'Type-Direct' search in Music.app first (Acoustic Precision)
    # FORCE STOP previous music to ensure we don't just 'resume' the old track
    run_applescript('tell application "Music" to stop')
    
    nav_script = f'''
    tell application "Music" to activate
    delay 0.5
    tell application "System Events"
        tell process "Music"
            -- Force open Search
            keystroke "f" using {{command down, option down}}
            delay 0.5
            keystroke "a" using {{command down}}
            key code 51 -- Delete
            keystroke "{clean_query}"
            delay 0.3
            key code 36 -- Enter trigger search
        end tell
    end tell
    '''
    run_applescript(nav_script)
    
    # 3. Autonomous Playback Trigger
    # Wait for results to load
    time.sleep(3.5)
    
    play_script = '''
    tell application "System Events"
        tell process "Music"
            -- Navigate results and force play the top hit
            key code 48 -- Tab (Highlight Search Result)
            delay 0.3
            key code 36 -- Enter (Force Play New Song)
        end tell
    end tell
    '''
    run_applescript(play_script)
    
    # Check if successful
    is_playing = run_applescript('tell application "Music" to get player state')
    if "playing" in is_playing:
        return "playing from search"
        
    # 4. Final Alexa Fallback: Web Streaming
    print("  [Music Tool] Local search yields no result. Transitioning to Web Stream (Alexa Logic)...")
    return play_on_web_service(query, service="youtube")

if __name__ == "__main__":
    # Test
    pass
