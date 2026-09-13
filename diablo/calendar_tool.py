"""
D.I.A.B.L.O. — Google Calendar Tool

Provides read/write access to Google Calendar via the official API.
Requires a credentials.json file from Google Cloud Console.

First-time setup:
  1. Go to https://console.cloud.google.com/
  2. Create a project, enable the Google Calendar API.
  3. Create OAuth 2.0 credentials (Desktop app).
  4. Download credentials.json and place it in the DIABLO directory.
  5. On first run, a browser window will open for OAuth consent.

SAFETY: Creating/modifying events requires verbal confirmation.
"""

import os
import datetime
from typing import Optional, List

CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CALENDAR_CREDENTIALS_PATH",
    "/Users/shaurya/.diablo/credentials.json"
)
TOKEN_PATH = "/Users/shaurya/.diablo/token.json"

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None


def _get_service():
    """Lazily builds and caches the Google Calendar API service."""
    global _service
    if _service is not None:
        return _service

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        return None

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    _service = build("calendar", "v3", credentials=creds)
    return _service


def is_available() -> bool:
    """Returns True if the Calendar tool is fully configured."""
    try:
        from googleapiclient.discovery import build  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(CREDENTIALS_PATH)


def get_todays_events() -> Optional[List[dict]]:
    """Fetches today's calendar events."""
    service = _get_service()
    if service is None:
        return None

    now = datetime.datetime.utcnow()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat() + "Z"

    result = service.events().list(
        calendarId="primary",
        timeMin=start_of_day,
        timeMax=end_of_day,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = result.get("items", [])
    return [
        {
            "summary": e.get("summary", "Untitled"),
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "end": e["end"].get("dateTime", e["end"].get("date")),
        }
        for e in events
    ]


def get_upcoming_events(n: int = 5) -> Optional[List[dict]]:
    """Fetches the next N upcoming events."""
    service = _get_service()
    if service is None:
        return None

    now = datetime.datetime.utcnow().isoformat() + "Z"
    result = service.events().list(
        calendarId="primary",
        timeMin=now,
        maxResults=n,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = result.get("items", [])
    return [
        {
            "summary": e.get("summary", "Untitled"),
            "start": e["start"].get("dateTime", e["start"].get("date")),
        }
        for e in events
    ]


def create_event(summary: str, start_iso: str, end_iso: str, timezone: str = "Asia/Kolkata") -> Optional[dict]:
    """
    Creates a new calendar event.

    Args:
        summary: Title of the event.
        start_iso: Start time in ISO format (e.g. '2026-04-14T15:00:00').
        end_iso: End time in ISO format.
        timezone: Timezone string (default: Asia/Kolkata for Pune).

    Returns:
        Created event dict or None on failure.
    """
    service = _get_service()
    if service is None:
        return None

    event = {
        "summary": summary,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
    }

    created = service.events().insert(calendarId="primary", body=event).execute()
    return {"summary": created["summary"], "link": created.get("htmlLink", "")}
