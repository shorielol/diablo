import subprocess
import time

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

def switch_to_site(site_keyword: str) -> bool:
    """
    Scans all open Zen windows for a keyword in their title.
    If found, focuses that window and returns True.
    """
    script = f'''
    tell application "System Events"
        if exists process "Zen" then
            tell process "Zen"
                set windowList to every window
                repeat with win in windowList
                    if name of win contains "{site_keyword.title()}" or name of win contains "{site_keyword.lower()}" then
                        set frontmost to true
                        perform action "AXRaise" of win
                        return "found"
                    end if
                end repeat
            end tell
        end if
    end tell
    return "not found"
    '''
    res = run_applescript(script)
    return res == "found"

def run_browser_navigation(url: str) -> bool:
    """
    Open a URL, and say whether it worked.

    Used to hardcode a browser name and ignore the exit code, which is how the
    project ran for months calling `open -a "Zen Browser"` — an application
    that does not exist under that name — while every tool above it reported
    success.
    """
    from skills import browsers
    return browsers.open_url(url)

def universal_scroll(direction: str = "down", amount: int = 1):
    """ Simulates keyboard scroll globally. """
    key_code = 125 if direction == "down" else 126
    if amount > 1:
        script = f'tell application "System Events" to repeat {amount} times \n key code {key_code} \n end repeat'
    else:
        script = f'tell application "System Events" to key code {key_code}'
    subprocess.run(["osascript", "-e", script])

def scroll_down(repeats: int = 3):
    universal_scroll("down", repeats)

def scroll_up(repeats: int = 3):
    universal_scroll("up", repeats)

def instagram_nav_to_messages():
    """Navigates to Instagram Messages, switching to tab if open."""
    if not switch_to_site("Instagram"):
        run_browser_navigation("https://www.instagram.com/direct/inbox/")

if __name__ == "__main__":
    # Test
    pass
