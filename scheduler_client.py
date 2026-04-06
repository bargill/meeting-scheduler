"""
Scheduler Client - Used by Cowork scheduled tasks to interact with the voting app API.

This module provides helper functions for:
- Creating meetings in the voting app
- Checking vote status
- Getting best time slots
- Finalizing meetings

Usage from Cowork scheduled tasks:
    from scheduler_client import SchedulerClient
    client = SchedulerClient("https://your-app.onrender.com")
    status = client.get_status(meeting_id)
"""

import json
import os
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional


class SchedulerClient:
    """Client for the Meeting Scheduler voting app API."""

    def __init__(self, base_url: str = None):
        self.base_url = (base_url or os.environ.get("SCHEDULER_URL", "http://localhost:5000")).rstrip("/")

    def create_meeting(
        self,
        title: str,
        organizer_name: str,
        organizer_email: str,
        duration_minutes: int,
        timeslots: list[dict],
        participants: list[dict],
        description: str = "",
    ) -> dict:
        """Create a new meeting and return participant voting links.

        Args:
            title: Meeting title
            organizer_name: Name of the organizer
            organizer_email: Email of the organizer
            duration_minutes: Meeting duration in minutes
            timeslots: List of {"start": "ISO datetime", "end": "ISO datetime"}
            participants: List of {"name": "...", "email": "..."}
            description: Optional meeting description

        Returns:
            dict with meeting_id, status, and participant_links
        """
        resp = requests.post(
            f"{self.base_url}/api/meetings",
            json={
                "title": title,
                "organizer_name": organizer_name,
                "organizer_email": organizer_email,
                "duration_minutes": duration_minutes,
                "description": description,
                "timeslots": timeslots,
                "participants": participants,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def get_meeting(self, meeting_id: str) -> dict:
        """Get full meeting details including vote tallies."""
        resp = requests.get(f"{self.base_url}/api/meetings/{meeting_id}")
        resp.raise_for_status()
        return resp.json()

    def get_status(self, meeting_id: str) -> dict:
        """Quick status check: who voted, who hasn't."""
        resp = requests.get(f"{self.base_url}/api/meetings/{meeting_id}/status")
        resp.raise_for_status()
        return resp.json()

    def get_best_slots(self, meeting_id: str) -> dict:
        """Get time slots ranked by availability."""
        resp = requests.get(f"{self.base_url}/api/meetings/{meeting_id}/best-slots")
        resp.raise_for_status()
        return resp.json()

    def finalize(self, meeting_id: str, finalized_slot: str = None, zoom_link: str = None) -> dict:
        """Mark meeting as finalized."""
        resp = requests.post(
            f"{self.base_url}/api/meetings/{meeting_id}/finalize",
            json={"finalized_slot": finalized_slot, "zoom_link": zoom_link},
        )
        resp.raise_for_status()
        return resp.json()

    def mark_reminded(self, meeting_id: str, emails: list[str]) -> dict:
        """Mark participants as reminded."""
        resp = requests.post(
            f"{self.base_url}/api/meetings/{meeting_id}/remind",
            json={"emails": emails},
        )
        resp.raise_for_status()
        return resp.json()

    def get_vote_url(self, meeting_id: str, token: str) -> str:
        """Build the full voting URL for a participant."""
        return f"{self.base_url}/vote/{meeting_id}?token={token}"


# ---------------------------------------------------------------------------
# Helpers for generating time slots from calendar availability
# ---------------------------------------------------------------------------

def generate_timeslots(
    free_windows: list[dict],
    duration_minutes: int,
    step_minutes: int = 30,
) -> list[dict]:
    """Generate possible meeting slots from free calendar windows.

    Args:
        free_windows: List of {"start": datetime, "end": datetime} representing
                      free periods from the calendar.
        duration_minutes: Required meeting length in minutes.
        step_minutes: Step size for generating slots (default 30 min).

    Returns:
        List of {"start": ISO string, "end": ISO string} time slot options.
    """
    slots = []
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)

    for window in free_windows:
        start = window["start"] if isinstance(window["start"], datetime) else datetime.fromisoformat(window["start"])
        end = window["end"] if isinstance(window["end"], datetime) else datetime.fromisoformat(window["end"])

        current = start
        while current + duration <= end:
            slots.append({
                "start": current.isoformat(),
                "end": (current + duration).isoformat(),
            })
            current += step

    return slots


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------

def compose_invitation_email(
    participant_name: str,
    meeting_title: str,
    organizer_name: str,
    duration_minutes: int,
    vote_url: str,
    description: str = "",
) -> dict:
    """Compose an invitation email for a participant.

    Returns:
        dict with 'subject' and 'body' (HTML) keys.
    """
    subject = f"When can you meet? \u2014 {meeting_title}"
    body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background: #4F46E5; color: white; padding: 24px; border-radius: 12px 12px 0 0; text-align: center;">
        <h1 style="margin: 0; font-size: 22px;">{meeting_title}</h1>
        <p style="margin: 8px 0 0; opacity: 0.9;">{duration_minutes} minutes &bull; Organized by {organizer_name}</p>
    </div>
    <div style="background: white; padding: 24px; border: 1px solid #E5E7EB; border-top: none; border-radius: 0 0 12px 12px;">
        <p style="color: #374151; font-size: 16px;">Hi {participant_name},</p>
        <p style="color: #374151; font-size: 16px;">{organizer_name} would like to schedule a meeting and needs to find a time that works for everyone. Please take a moment to mark your available time slots.</p>
        {"<p style='color: #6B7280; font-size: 14px; border-left: 3px solid #E5E7EB; padding-left: 12px; margin: 16px 0;'>" + description + "</p>" if description else ""}
        <div style="text-align: center; margin: 24px 0;">
            <a href="{vote_url}" style="display: inline-block; background: #4F46E5; color: white; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 16px;">Pick Your Available Times</a>
        </div>
        <p style="color: #9CA3AF; font-size: 13px; text-align: center;">This link is unique to you. Please don't share it with others.</p>
    </div>
</div>"""
    return {"subject": subject, "body": body}


def compose_reminder_email(
    participant_name: str,
    meeting_title: str,
    organizer_name: str,
    vote_url: str,
) -> dict:
    """Compose a gentle reminder email."""
    subject = f"Friendly reminder: pick your times for {meeting_title}"
    body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background: white; padding: 24px; border: 1px solid #E5E7EB; border-radius: 12px;">
        <p style="color: #374151; font-size: 16px;">Hi {participant_name},</p>
        <p style="color: #374151; font-size: 16px;">Just a friendly reminder that {organizer_name} is still waiting for your availability for <strong>{meeting_title}</strong>. It'll only take a minute!</p>
        <div style="text-align: center; margin: 24px 0;">
            <a href="{vote_url}" style="display: inline-block; background: #4F46E5; color: white; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 16px;">Pick Your Times</a>
        </div>
    </div>
</div>"""
    return {"subject": subject, "body": body}


def compose_confirmation_email(
    participant_name: str,
    meeting_title: str,
    organizer_name: str,
    start_time: str,
    end_time: str,
    zoom_link: str = None,
) -> dict:
    """Compose a meeting confirmation email."""
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)
    date_str = start_dt.strftime("%A, %B %d, %Y")
    time_str = f"{start_dt.strftime('%H:%M')} \u2013 {end_dt.strftime('%H:%M')}"

    zoom_section = ""
    if zoom_link:
        zoom_section = f"""
        <div style="background: #EEF2FF; padding: 16px; border-radius: 8px; margin: 16px 0; text-align: center;">
            <p style="margin: 0 0 8px; color: #374151; font-weight: 600;">Zoom Meeting</p>
            <a href="{zoom_link}" style="color: #4F46E5; font-size: 14px; word-break: break-all;">{zoom_link}</a>
        </div>"""

    subject = f"Confirmed: {meeting_title} \u2014 {date_str}"
    body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background: #10B981; color: white; padding: 24px; border-radius: 12px 12px 0 0; text-align: center;">
        <h1 style="margin: 0; font-size: 22px;">Meeting Confirmed!</h1>
    </div>
    <div style="background: white; padding: 24px; border: 1px solid #E5E7EB; border-top: none; border-radius: 0 0 12px 12px;">
        <p style="color: #374151; font-size: 16px;">Hi {participant_name},</p>
        <p style="color: #374151; font-size: 16px;">Great news! <strong>{meeting_title}</strong> has been scheduled:</p>
        <div style="background: #F9FAFB; padding: 16px; border-radius: 8px; margin: 16px 0;">
            <p style="margin: 0; color: #374151; font-weight: 600;">{date_str}</p>
            <p style="margin: 4px 0 0; color: #6B7280;">{time_str}</p>
        </div>
        {zoom_section}
        <p style="color: #6B7280; font-size: 14px;">A calendar invitation has been sent separately. See you there!</p>
    </div>
</div>"""
    return {"subject": subject, "body": body}
