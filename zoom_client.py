"""
Zoom Integration - Create Zoom meetings via the Zoom API.

Setup required:
1. Go to https://marketplace.zoom.us/develop/create
2. Create a "Server-to-Server OAuth" app
3. Note down: Account ID, Client ID, Client Secret
4. Set these as environment variables:
   - ZOOM_ACCOUNT_ID
   - ZOOM_CLIENT_ID
   - ZOOM_CLIENT_SECRET

The Server-to-Server OAuth flow is the simplest for automated meeting creation
(no user login required).
"""

import os
import requests
from datetime import datetime
from typing import Optional


class ZoomClient:
    """Client for creating Zoom meetings via Server-to-Server OAuth."""

    TOKEN_URL = "https://zoom.us/oauth/token"
    API_BASE = "https://api.zoom.us/v2"

    def __init__(
        self,
        account_id: str = None,
        client_id: str = None,
        client_secret: str = None,
    ):
        self.account_id = account_id or os.environ.get("ZOOM_ACCOUNT_ID", "")
        self.client_id = client_id or os.environ.get("ZOOM_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("ZOOM_CLIENT_SECRET", "")
        self._access_token = None

    @property
    def is_configured(self) -> bool:
        return bool(self.account_id and self.client_id and self.client_secret)

    def _get_access_token(self) -> str:
        """Obtain an access token using Server-to-Server OAuth."""
        if self._access_token:
            return self._access_token

        resp = requests.post(
            self.TOKEN_URL,
            params={"grant_type": "account_credentials", "account_id": self.account_id},
            auth=(self.client_id, self.client_secret),
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        return self._access_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
        }

    def create_meeting(
        self,
        topic: str,
        start_time: str,
        duration_minutes: int,
        timezone: str = "Asia/Jerusalem",
        agenda: str = "",
        participant_emails: Optional[list[str]] = None,
    ) -> dict:
        """Create a Zoom meeting.

        Args:
            topic: Meeting title.
            start_time: ISO 8601 datetime string (e.g. "2026-04-10T09:00:00").
            duration_minutes: Meeting duration.
            timezone: IANA timezone name.
            agenda: Optional description.
            participant_emails: Optional list of participant emails (for calendar invites).

        Returns:
            dict with keys: id, join_url, start_url, password, etc.
        """
        body = {
            "topic": topic,
            "type": 2,  # Scheduled meeting
            "start_time": start_time,
            "duration": duration_minutes,
            "timezone": timezone,
            "agenda": agenda,
            "settings": {
                "host_video": True,
                "participant_video": True,
                "join_before_host": True,
                "waiting_room": False,
                "auto_recording": "none",
                "meeting_authentication": False,
            },
        }

        # Add calendar invitees if provided
        if participant_emails:
            body["settings"]["meeting_invitees"] = [
                {"email": email} for email in participant_emails
            ]

        resp = requests.post(
            f"{self.API_BASE}/users/me/meetings",
            headers=self._headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "id": data["id"],
            "join_url": data["join_url"],
            "start_url": data["start_url"],
            "password": data.get("password", ""),
            "topic": data["topic"],
            "start_time": data["start_time"],
            "duration": data["duration"],
        }
