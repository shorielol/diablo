import subprocess
import time
import webbrowser
from typing import List, Optional

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

def play_pause():
    return run_applescript('tell application "Music" to playpause')

def next_track():
    return run_applescript('tell application "Music" to next track')

def previous_track():
    return run_applescript('tell application "Music" to previous track')

def play_on_web_service(query: str, service: str = "youtube"):
    """
    Alexa-style streaming: Opens preferred web service and triggers playback.
    """
    clean_query = query.replace(" ", "+")
    if service == "spotify":
        url = f"https://open.spotify.com/search/{clean_query}"
    else: # YouTube Music default
        url = f"https://music.youtube.com/search?q={clean_query}"
    
    print(f"  [Music Tool] Streaming from {service.title()}: {url}")
    # Using 'open' command for Zen Browser compatibility via system default
    subprocess.run(["open", "-a", "Zen Browser", url])
    
    # Wait for page load and attempt to trigger 'Play' (service specific)
    time.sleep(4.0)
    if service == "youtube":
        subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 36']) # Enter
        time.sleep(1.0)
        subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 36']) # Enter again
    elif service == "spotify":
        subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 36']) # Enter
    
    return f"streaming on {service}"

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
