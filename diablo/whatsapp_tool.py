import pyautogui
import time
import subprocess
import os

# Set fail-safe to move mouse to corner to abort
pyautogui.FAILSAFE = True

def focus_zen():
    """Brings Zen browser to the frontmost layer."""
    script = 'tell application "System Events" to set frontmost of process "Zen" to true'
    subprocess.run(["osascript", "-e", script])
    time.sleep(0.5)

def send_whatsapp_msg(recipient: str, message: str):
    """
    Automates WhatsApp Web (Zen Browser) to send a message.
    Force-navigates to web.whatsapp.com first.
    """
    print(f"  [WhatsApp Tool] Starting message to {recipient}...")
    
    # 1. FORCE NAVIGATION
    nav_script = f'tell application "Zen" to open location "https://web.whatsapp.com"'
    subprocess.run(["osascript", "-e", nav_script])
    time.sleep(5.0) # WhatsApp Web takes a while to load properly
    
    focus_zen()
    
    # 2. Focus Search Bar
    # Shortcut for search in WhatsApp Web is often Cmd+Alt+/ or just clicking
    # We'll try hitting 'Esc' to clear any state then use search shortcut
    pyautogui.press('esc')
    time.sleep(0.3)
    
    # Search shortcut on macOS for WhatsApp Web
    pyautogui.hotkey('command', 'option', '/')
    time.sleep(0.5)
    
    # Select All + Clear
    pyautogui.hotkey('command', 'a')
    pyautogui.press('backspace')
    time.sleep(0.2)
    
    # 3. Type name
    print(f"  [WhatsApp Tool] Searching for {recipient}...")
    pyautogui.write(recipient, interval=0.1)
    time.sleep(2.0) # Wait for search results
    
    # 4. Select first result (usually Enter works if search is exclusive)
    pyautogui.press('enter')
    time.sleep(1.0) # Wait for chat window to load
    
    # 5. Type message and send
    print(f"  [WhatsApp Tool] Sending message...")
    pyautogui.write(message, interval=0.08)
    time.sleep(0.3)
    pyautogui.press('enter')
    
    return f"WhatsApp message sent to {recipient}, Sir."

if __name__ == "__main__":
    # Test
    pass
