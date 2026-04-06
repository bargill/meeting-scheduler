"""
Meeting Scheduler - Voting Web App
A Flask application that serves voting pages for meeting scheduling.
Participants receive a unique link, pick their available time slots, and
votes are collected for the organizer to finalize.
"""

import os
import uuid
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

from flask import Flask, request, jsonify, render_template, g, abort

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

DATABASE = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "scheduler.db"))

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript(SCHEMA)
    db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    organizer_name  TEXT NOT NULL,
    organizer_email TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'voting',
    finalized_slot  TEXT,
    zoom_link       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeslots (
    id          TEXT PRIMARY KEY,
    meeting_id  TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    id          TEXT PRIMARY KEY,
    meeting_id  TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    has_voted   INTEGER NOT NULL DEFAULT 0,
    reminded_at TEXT
);

CREATE TABLE IF NOT EXISTS votes (
    id              TEXT PRIMARY KEY,
    participant_id  TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    timeslot_id     TEXT NOT NULL REFERENCES timeslots(id) ON DELETE CASCADE,
    available       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(participant_id, timeslot_id)
);
"""

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/meetings", methods=["POST"])
def create_meeting():
    """Create a new meeting with time slots and participants.

    Expected JSON body:
    {
        "title": "Weekly sync",
        "organizer_name": "Nir",
        "organizer_email": "bargill@gmail.com",
        "duration_minutes": 60,
        "description": "Optional description",
        "timeslots": [
            {"start": "2026-04-10T09:00:00+03:00", "end": "2026-04-10T10:00:00+03:00"},
            ...
        ],
        "participants": [
            {"name": "Alice", "email": "alice@example.com"},
            ...
        ]
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["title", "organizer_name", "organizer_email", "duration_minutes", "timeslots", "participants"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing field: {field}"}), 400

    now = datetime.now(timezone.utc).isoformat()
    meeting_id = str(uuid.uuid4())

    db = get_db()

    db.execute(
        """INSERT INTO meetings (id, title, organizer_name, organizer_email,
           duration_minutes, description, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'voting', ?, ?)""",
        (meeting_id, data["title"], data["organizer_name"], data["organizer_email"],
         data["duration_minutes"], data.get("description", ""), now, now),
    )

    timeslot_ids = []
    for slot in data["timeslots"]:
        ts_id = str(uuid.uuid4())
        timeslot_ids.append(ts_id)
        db.execute(
            "INSERT INTO timeslots (id, meeting_id, start_time, end_time) VALUES (?, ?, ?, ?)",
            (ts_id, meeting_id, slot["start"], slot["end"]),
        )

    participant_links = []
    for p in data["participants"]:
        p_id = str(uuid.uuid4())
        token = str(uuid.uuid4())
        db.execute(
            "INSERT INTO participants (id, meeting_id, name, email, token) VALUES (?, ?, ?, ?, ?)",
            (p_id, meeting_id, p["name"], p["email"], token),
        )
        participant_links.append({
            "name": p["name"],
            "email": p["email"],
            "token": token,
            "vote_url": f"/vote/{meeting_id}?token={token}",
        })

    db.commit()

    return jsonify({
        "meeting_id": meeting_id,
        "status": "voting",
        "participant_links": participant_links,
    }), 201


@app.route("/api/meetings/<meeting_id>", methods=["GET"])
def get_meeting(meeting_id):
    """Get full meeting details including vote tallies."""
    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    timeslots = db.execute(
        "SELECT * FROM timeslots WHERE meeting_id = ? ORDER BY start_time", (meeting_id,)
    ).fetchall()

    participants = db.execute(
        "SELECT * FROM participants WHERE meeting_id = ?", (meeting_id,)
    ).fetchall()

    # Build vote tally
    tally = {}
    for ts in timeslots:
        votes = db.execute(
            "SELECT v.available, p.name FROM votes v JOIN participants p ON v.participant_id = p.id WHERE v.timeslot_id = ?",
            (ts["id"],),
        ).fetchall()
        available_names = [v["name"] for v in votes if v["available"]]
        tally[ts["id"]] = {
            "start": ts["start_time"],
            "end": ts["end_time"],
            "available_count": len(available_names),
            "available_names": available_names,
            "total_participants": len(participants),
        }

    return jsonify({
        "meeting": dict(meeting),
        "timeslots": tally,
        "participants": [
            {"name": p["name"], "email": p["email"], "has_voted": bool(p["has_voted"]),
             "token": p["token"]}
            for p in participants
        ],
    })


@app.route("/api/meetings/<meeting_id>/status", methods=["GET"])
def meeting_status(meeting_id):
    """Quick status check: who has voted, who hasn't."""
    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    participants = db.execute(
        "SELECT name, email, has_voted, reminded_at, token FROM participants WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchall()

    voted = [p for p in participants if p["has_voted"]]
    pending = [p for p in participants if not p["has_voted"]]

    return jsonify({
        "meeting_id": meeting_id,
        "title": meeting["title"],
        "status": meeting["status"],
        "total_participants": len(participants),
        "voted_count": len(voted),
        "voted": [{"name": p["name"], "email": p["email"]} for p in voted],
        "pending": [
            {"name": p["name"], "email": p["email"], "token": p["token"],
             "reminded_at": p["reminded_at"]}
            for p in pending
        ],
        "all_voted": len(pending) == 0,
    })


@app.route("/api/meetings/<meeting_id>/best-slots", methods=["GET"])
def best_slots(meeting_id):
    """Return time slots ranked by number of available participants."""
    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    timeslots = db.execute(
        "SELECT * FROM timeslots WHERE meeting_id = ? ORDER BY start_time", (meeting_id,)
    ).fetchall()

    total_participants = db.execute(
        "SELECT COUNT(*) as cnt FROM participants WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()["cnt"]

    results = []
    for ts in timeslots:
        available_count = db.execute(
            "SELECT COUNT(*) as cnt FROM votes WHERE timeslot_id = ? AND available = 1",
            (ts["id"],),
        ).fetchone()["cnt"]
        available_names = [
            r["name"] for r in db.execute(
                "SELECT p.name FROM votes v JOIN participants p ON v.participant_id = p.id "
                "WHERE v.timeslot_id = ? AND v.available = 1", (ts["id"],)
            ).fetchall()
        ]
        results.append({
            "timeslot_id": ts["id"],
            "start": ts["start_time"],
            "end": ts["end_time"],
            "available_count": available_count,
            "total_participants": total_participants,
            "available_names": available_names,
            "everyone_available": available_count == total_participants,
        })

    results.sort(key=lambda x: x["available_count"], reverse=True)
    return jsonify({"meeting_id": meeting_id, "ranked_slots": results})


@app.route("/api/meetings/<meeting_id>/finalize", methods=["POST"])
def finalize_meeting(meeting_id):
    """Mark a meeting as finalized with the chosen slot and optional zoom link."""
    data = request.get_json() or {}
    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE meetings SET status = 'finalized', finalized_slot = ?, zoom_link = ?, updated_at = ? WHERE id = ?",
        (data.get("finalized_slot"), data.get("zoom_link"), now, meeting_id),
    )
    db.commit()
    return jsonify({"status": "finalized", "meeting_id": meeting_id})


@app.route("/api/meetings/<meeting_id>/remind", methods=["POST"])
def mark_reminded(meeting_id):
    """Mark specific participants as reminded. Body: {"emails": ["a@b.com"]}"""
    data = request.get_json() or {}
    emails = data.get("emails", [])
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    for email in emails:
        db.execute(
            "UPDATE participants SET reminded_at = ? WHERE meeting_id = ? AND email = ?",
            (now, meeting_id, email),
        )
    db.commit()
    return jsonify({"reminded": emails})


# ---------------------------------------------------------------------------
# Voting Page (participant-facing)
# ---------------------------------------------------------------------------

@app.route("/vote/<meeting_id>", methods=["GET"])
def vote_page(meeting_id):
    """Render the voting page for a participant."""
    token = request.args.get("token")
    if not token:
        abort(400, "Missing token parameter")

    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not meeting:
        abort(404, "Meeting not found")

    if meeting["status"] != "voting":
        return render_template("vote_closed.html", meeting=meeting)

    participant = db.execute(
        "SELECT * FROM participants WHERE meeting_id = ? AND token = ?",
        (meeting_id, token),
    ).fetchone()
    if not participant:
        abort(403, "Invalid voting link")

    timeslots = db.execute(
        "SELECT * FROM timeslots WHERE meeting_id = ? ORDER BY start_time",
        (meeting_id,),
    ).fetchall()

    # Check for existing votes
    existing_votes = {}
    if participant["has_voted"]:
        votes = db.execute(
            "SELECT timeslot_id, available FROM votes WHERE participant_id = ?",
            (participant["id"],),
        ).fetchall()
        existing_votes = {v["timeslot_id"]: v["available"] for v in votes}

    return render_template(
        "vote.html",
        meeting=meeting,
        participant=participant,
        timeslots=timeslots,
        existing_votes=existing_votes,
    )


@app.route("/vote/<meeting_id>/submit", methods=["POST"])
def submit_vote(meeting_id):
    """Process vote submission from the voting page."""
    token = request.form.get("token")
    if not token:
        return jsonify({"error": "Missing token"}), 400

    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not meeting or meeting["status"] != "voting":
        return jsonify({"error": "Voting is closed"}), 400

    participant = db.execute(
        "SELECT * FROM participants WHERE meeting_id = ? AND token = ?",
        (meeting_id, token),
    ).fetchone()
    if not participant:
        return jsonify({"error": "Invalid token"}), 403

    timeslots = db.execute(
        "SELECT id FROM timeslots WHERE meeting_id = ?", (meeting_id,)
    ).fetchall()

    # Delete any existing votes for re-submission
    db.execute("DELETE FROM votes WHERE participant_id = ?", (participant["id"],))

    # Insert new votes
    selected_ids = request.form.getlist("slots")
    for ts in timeslots:
        vote_id = str(uuid.uuid4())
        available = 1 if ts["id"] in selected_ids else 0
        db.execute(
            "INSERT INTO votes (id, participant_id, timeslot_id, available) VALUES (?, ?, ?, ?)",
            (vote_id, participant["id"], ts["id"], available),
        )

    db.execute(
        "UPDATE participants SET has_voted = 1 WHERE id = ?", (participant["id"],)
    )
    db.commit()

    return render_template("vote_thanks.html", meeting=meeting, participant=participant)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
