"""
D.I.A.B.L.O. — Communications Tool (iMessage via AppleScript)

Uses macOS native Messages app to send iMessages.
No API keys or third-party services required.

SAFETY: Every message must be verbally confirmed before sending.
"""

import subprocess
import re
from typing import Optional


def send_imessage(recipient: str, message: str) -> dict:
    """
    Sends an iMessage via the macOS Messages app using AppleScript.

    Args:
        recipient: Phone number or email address of the contact.
        message: The text message to send.

    Returns:
        dict with 'success' (bool) and 'detail' (str).
    """
    # Sanitise inputs to prevent AppleScript injection
    safe_recipient = recipient.replace('"', '\\"').replace("'", "\\'")
    safe_message = message.replace('"', '\\"').replace("'", "\\'")

    script = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{safe_recipient}" of targetService
        send "{safe_message}" to targetBuddy
    end tell
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return {"success": True, "detail": f"Message sent to {recipient}."}
        else:
            return {"success": False, "detail": result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"success": False, "detail": "AppleScript timed out."}
    except Exception as e:
        return {"success": False, "detail": str(e)}


def get_contact_name_from_text(text: str) -> Optional[str]:
    """
    Tries to extract a contact name/number from natural language.
    e.g. "send a message to Mom saying hello" -> "Mom"
    """
    patterns = [
        r"(?:message|text|send)\s+(?:to\s+)?(.+?)\s+(?:saying|that|with)",
        r"(?:message|text|send)\s+(?:to\s+)?(.+?)$",
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def get_message_body_from_text(text: str) -> Optional[str]:
    """
    Extracts the message body from natural language.
    e.g. "send a message to Mom saying I'll be late" -> "I'll be late"
    """
    patterns = [
        r"(?:saying|that says|with message|with text)\s+(.+)$",
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None
