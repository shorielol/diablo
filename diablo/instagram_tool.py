import pyautogui
import time
import subprocess
import os

# Set fail-safe to move mouse to corner to abort
pyautogui.FAILSAFE = True

def focus_zen():
    """Brings Zen browser to the frontmost layer and ensures we are on the DM page."""
    # Force focus
    script = 'tell application "System Events" to set frontmost of process "Zen" to true'
    subprocess.run(["osascript", "-e", script])
    time.sleep(0.5)

def send_ig_reply(recipient: str, message: str):
    """
    Automates Instagram Web (Zen Browser) to send a reply.
    Now force-navigates to the DM inbox first for maximum reliability.
    """
    print(f"  [IG Tool] Starting reply to {recipient}...")
    
    # 1. FORCE NAVIGATION FIRST
    # This ensures we are always looking at the Direct Messages page
    # Using open location via AppleScript to keep it fast
    nav_script = f'tell application "Zen" to open location "https://www.instagram.com/direct/inbox/"'
    subprocess.run(["osascript", "-e", nav_script])
    time.sleep(2.5) # Wait for page load
    
    focus_zen()
    
    # 2. Focus Search
    pyautogui.press('esc')
    time.sleep(0.3)
    pyautogui.press('/') 
    time.sleep(0.5)
    
    # Select All + Clear
    pyautogui.hotkey('command', 'a')
    pyautogui.press('backspace')
    time.sleep(0.2)
    
    # 3. Type recipient
    print(f"  [IG Tool] Searching for {recipient}...")
    pyautogui.write(recipient, interval=0.1)
    time.sleep(2.0) # Wait for contacts to populate
    
    # 4. Select first result
    pyautogui.press('down')
    time.sleep(0.3)
    pyautogui.press('enter')
    time.sleep(1.5) # Wait for the chat window to load
    
    # 5. Type message and send
    print(f"  [IG Tool] Sending message...")
    pyautogui.write(message, interval=0.08)
    time.sleep(0.3)
    pyautogui.press('enter')
    
    return f"Processed Instagram reply to {recipient}, Sir."

if __name__ == "__main__":
    # Test
    pass
