"""
Meeting Link Helper

Three strategies for generating video meeting links — pick one via
`meeting_link_type` in config.json:

  "zoom_browser"   — Claude in Chrome navigates zoom.us to schedule a real
                     meeting with a unique link per meeting. No API needed.
                     (Recommended for Zoom accounts without API access.)

  "zoom_pmr"       — Uses your fixed Personal Meeting Room URL from
                     zoom_personal_meeting_url in config.json. Simple, but
                     every meeting shares the same link.

  "google_meet"    — Auto-generated via Google Calendar conferenceData.
                     Zero setup; link is created when the calendar event is.

  "jitsi"          — meet.jit.si room, no account required.
                     Good as a last resort fallback.
"""

import re
import uuid


# ── Google Meet ──────────────────────────────────────────────────────────────

def make_conference_data(request_id: str = None) -> dict:
    """
    conferenceData block for gcal_create_event.
    Google automatically creates a Meet link inside the calendar event.

    Usage:
        event["conferenceData"] = make_conference_data()
        result = gcal_create_event(event=event, sendUpdates="all")
        link = extract_meet_link(result)
    """
    return {
        "createRequest": {
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
            "requestId": request_id or str(uuid.uuid4()),
        }
    }


def extract_meet_link(gcal_event_response: dict) -> str | None:
    """Pull the Google Meet join URL from a gcal_create_event response."""
    conf = gcal_event_response.get("conferenceData") or {}
    for ep in conf.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            return ep.get("uri")
    return gcal_event_response.get("hangoutLink")


# ── Jitsi Meet ───────────────────────────────────────────────────────────────

def jitsi_url(meeting_title: str) -> str:
    """
    Generate an unguessable Jitsi Meet URL. No account required.

    Example:
        jitsi_url("Q2 Planning")  →  "https://meet.jit.si/Q2Planning-a3f8"
    """
    slug = re.sub(r"[^A-Za-z0-9]", "", meeting_title.title().replace(" ", ""))[:30]
    suffix = uuid.uuid4().hex[:4]
    return f"https://meet.jit.si/{slug}-{suffix}"


# ── Zoom browser automation instructions ─────────────────────────────────────
#
# When meeting_link_type == "zoom_browser", there is no Python code to call.
# Instead, the Cowork scheduled task / skill uses Claude in Chrome to navigate
# zoom.us and schedule the meeting. The steps are documented in the skill and
# scheduled task prompts.
#
# Quick reference for the automation sequence:
#   1. navigate("https://zoom.us/meeting/schedule")
#   2. Fill: Topic, Date/Time, Duration, Timezone
#   3. Uncheck "Waiting Room" if desired
#   4. Save → copy the "Join URL" from the confirmation page
#
# The join URL looks like: https://zoom.us/j/1234567890?pwd=xxxxx
#
# ── Zoom Personal Meeting Room ────────────────────────────────────────────────

def zoom_pmr_url(config: dict) -> str | None:
    """Return the user's fixed Personal Meeting Room URL from config."""
    return config.get("zoom_personal_meeting_url")
