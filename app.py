"""
Meeting Scheduler - Voting Web App
A Flask application that serves voting pages for meeting scheduling.
Participants receive a unique link, pick their available time slots, and
votes are collected for the organizer to finalize.

Supports PostgreSQL (production) and SQLite (local development).
Set DATABASE_URL env var for PostgreSQL, otherwise falls back to SQLite.
"""

import os
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from urllib.parse import urlparse

from flask import Flask, request, jsonify, render_template, g, abort

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

DATABASE_URL = os.environ.get("DATABASE_URL")
SQLITE_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "scheduler.db"))

# Detect which database backend to use
USE_POSTGRES = DATABASE_URL is not None

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Render uses postgres:// but psycopg2 needs postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    import sqlite3


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            g.db = psycopg2.connect(DATABASE_URL)
            g.db.autocommit = False
        else:
            g.db = sqlite3.connect(SQLITE_PATH)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA journal_mode=WAL")
            g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def db_execute(query, params=None):
    """Execute a query, handling parameter style differences between SQLite (?) and Postgres (%s)."""
    db = get_db()
    if USE_POSTGRES:
        # Convert ? placeholders to %s for psycopg2
        query = query.replace("?", "%s")
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = db.cursor()
    cur.execute(query, params or ())
    return cur


def db_fetchone(query, params=None):
    cur = db_execute(query, params)
    row = cur.fetchone()
    cur.close()
    return row


def db_fetchall(query, params=None):
    cur = db_execute(query, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def db_commit():
    get_db().commit()


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_POSTGRES = """
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

SCHEMA_SQLITE = SCHEMA_POSTGRES  # Same schema works for both


def init_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # Execute each statement separately for PostgreSQL
        for statement in SCHEMA_POSTGRES.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)
        conn.commit()
        cur.close()
        conn.close()
    else:
        import sqlite3 as sq
        db = sq.connect(SQLITE_PATH)
        db.executescript(SCHEMA_SQLITE)
        db.close()


# ---------------------------------------------------------------------------
# Helper to convert row to dict (works for both backends)
# ---------------------------------------------------------------------------

def row_to_dict(row):
    """Convert a database row to a dictionary."""
    if row is None:
        return None
    if USE_POSTGRES:
        return dict(row)  # RealDictCursor already returns dict-like
    else:
        return dict(row)  # sqlite3.Row supports dict()


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/meetings", methods=["POST"])
def create_meeting():
    """Create a new meeting with time slots and participants."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["title", "organizer_name", "organizer_email", "duration_minutes", "timeslots", "participants"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing field: {field}"}), 400

    now = datetime.now(timezone.utc).isoformat()
    meeting_id = str(uuid.uuid4())

    db_execute(
        """INSERT INTO meetings (id, title, organizer_name, organizer_email,
           duration_minutes, description, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'voting', ?, ?)""",
        (meeting_id, data["title"], data["organizer_name"], data["organizer_email"],
         data["duration_minutes"], data.get("description", ""), now, now),
    )

    for slot in data["timeslots"]:
        ts_id = str(uuid.uuid4())
        db_execute(
            "INSERT INTO timeslots (id, meeting_id, start_time, end_time) VALUES (?, ?, ?, ?)",
            (ts_id, meeting_id, slot["start"], slot["end"]),
        )

    participant_links = []
    for p in data["participants"]:
        p_id = str(uuid.uuid4())
        token = str(uuid.uuid4())
        db_execute(
            "INSERT INTO participants (id, meeting_id, name, email, token) VALUES (?, ?, ?, ?, ?)",
            (p_id, meeting_id, p["name"], p["email"], token),
        )
        participant_links.append({
            "name": p["name"],
            "email": p["email"],
            "token": token,
            "vote_url": f"/vote/{meeting_id}?token={token}",
        })

    db_commit()

    return jsonify({
        "meeting_id": meeting_id,
        "status": "voting",
        "participant_links": participant_links,
    }), 201


@app.route("/api/meetings", methods=["GET"])
def list_meetings():
    """List all meetings (summary), optionally filtered by status."""
    status_filter = request.args.get("status")  # e.g. ?status=voting
    if status_filter:
        rows = db_fetchall(
            "SELECT * FROM meetings WHERE status = ? ORDER BY created_at DESC", (status_filter,)
        )
    else:
        rows = db_fetchall("SELECT * FROM meetings ORDER BY created_at DESC")
    return jsonify({"meetings": [row_to_dict(r) for r in rows]})


@app.route("/api/meetings/<meeting_id>", methods=["GET"])
def get_meeting(meeting_id):
    """Get full meeting details including vote tallies."""
    meeting = db_fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    meeting = row_to_dict(meeting)

    timeslots = db_fetchall(
        "SELECT * FROM timeslots WHERE meeting_id = ? ORDER BY start_time", (meeting_id,)
    )

    participants = db_fetchall(
        "SELECT * FROM participants WHERE meeting_id = ?", (meeting_id,)
    )

    # Build vote tally
    tally = {}
    for ts in timeslots:
        ts = row_to_dict(ts)
        votes = db_fetchall(
            "SELECT v.available, p.name FROM votes v JOIN participants p ON v.participant_id = p.id WHERE v.timeslot_id = ?",
            (ts["id"],),
        )
        available_names = [row_to_dict(v)["name"] for v in votes if row_to_dict(v)["available"]]
        tally[ts["id"]] = {
            "start": ts["start_time"],
            "end": ts["end_time"],
            "available_count": len(available_names),
            "available_names": available_names,
            "total_participants": len(participants),
        }

    return jsonify({
        "meeting": meeting,
        "timeslots": tally,
        "participants": [
            {"name": row_to_dict(p)["name"], "email": row_to_dict(p)["email"],
             "has_voted": bool(row_to_dict(p)["has_voted"]), "token": row_to_dict(p)["token"]}
            for p in participants
        ],
    })


@app.route("/api/meetings/<meeting_id>/status", methods=["GET"])
def meeting_status(meeting_id):
    """Quick status check: who has voted, who hasn't."""
    meeting = db_fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    meeting = row_to_dict(meeting)

    participants = db_fetchall(
        "SELECT name, email, has_voted, reminded_at, token FROM participants WHERE meeting_id = ?",
        (meeting_id,),
    )
    participants = [row_to_dict(p) for p in participants]

    voted = [p for p in participants if p["has_voted"]]
    pending = [p for p in participants if not p["has_voted"]]

    return jsonify({
        "meeting_id": meeting_id,
        "title": meeting["title"],
        "status": meeting["status"],
        "duration_minutes": meeting["duration_minutes"],
        "description": meeting.get("description", ""),
        "created_at": meeting["created_at"],
        "finalized_slot": meeting.get("finalized_slot"),
        "zoom_link": meeting.get("zoom_link"),
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


@app.route("/api/meetings/<meeting_id>/votes-grid", methods=["GET"])
def votes_grid(meeting_id):
    """Return a full participant × timeslot vote matrix for the dashboard grid view."""
    meeting = db_fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404
    meeting = row_to_dict(meeting)

    timeslots = [row_to_dict(r) for r in db_fetchall(
        "SELECT * FROM timeslots WHERE meeting_id = ? ORDER BY start_time", (meeting_id,)
    )]
    participants = [row_to_dict(r) for r in db_fetchall(
        "SELECT * FROM participants WHERE meeting_id = ?", (meeting_id,)
    )]

    # Build lookup: (participant_id, timeslot_id) -> available (0/1)
    all_votes = db_fetchall(
        """SELECT v.participant_id, v.timeslot_id, v.available
           FROM votes v
           JOIN participants p ON v.participant_id = p.id
           WHERE p.meeting_id = ?""",
        (meeting_id,),
    )
    vote_map = {(row_to_dict(v)["participant_id"], row_to_dict(v)["timeslot_id"]): row_to_dict(v)["available"]
                for v in all_votes}

    grid_participants = []
    for p in participants:
        row = {"id": p["id"], "name": p["name"], "email": p["email"],
               "has_voted": bool(p["has_voted"]), "votes": {}}
        for ts in timeslots:
            key = (p["id"], ts["id"])
            if not p["has_voted"]:
                row["votes"][ts["id"]] = "pending"
            elif key in vote_map:
                row["votes"][ts["id"]] = "yes" if vote_map[key] else "no"
            else:
                row["votes"][ts["id"]] = "no"
        grid_participants.append(row)

    return jsonify({
        "meeting_id": meeting_id,
        "title": meeting["title"],
        "status": meeting["status"],
        "finalized_slot": meeting.get("finalized_slot"),
        "zoom_link": meeting.get("zoom_link"),
        "timeslots": [{"id": ts["id"], "start": ts["start_time"], "end": ts["end_time"]}
                      for ts in timeslots],
        "participants": grid_participants,
    })


@app.route("/api/meetings/<meeting_id>/best-slots", methods=["GET"])
def best_slots(meeting_id):
    """Return time slots ranked by number of available participants."""
    meeting = db_fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    timeslots = db_fetchall(
        "SELECT * FROM timeslots WHERE meeting_id = ? ORDER BY start_time", (meeting_id,)
    )

    total_participants = db_fetchone(
        "SELECT COUNT(*) as cnt FROM participants WHERE meeting_id = ?", (meeting_id,)
    )
    total_count = row_to_dict(total_participants)["cnt"]

    results = []
    for ts in timeslots:
        ts = row_to_dict(ts)
        available_count_row = db_fetchone(
            "SELECT COUNT(*) as cnt FROM votes WHERE timeslot_id = ? AND available = 1",
            (ts["id"],),
        )
        available_count = row_to_dict(available_count_row)["cnt"]
        available_names = [
            row_to_dict(r)["name"] for r in db_fetchall(
                "SELECT p.name FROM votes v JOIN participants p ON v.participant_id = p.id "
                "WHERE v.timeslot_id = ? AND v.available = 1", (ts["id"],)
            )
        ]
        results.append({
            "timeslot_id": ts["id"],
            "start": ts["start_time"],
            "end": ts["end_time"],
            "available_count": available_count,
            "total_participants": total_count,
            "available_names": available_names,
            "everyone_available": available_count == total_count,
        })

    results.sort(key=lambda x: x["available_count"], reverse=True)
    return jsonify({"meeting_id": meeting_id, "ranked_slots": results})


@app.route("/api/meetings/<meeting_id>/finalize", methods=["POST"])
def finalize_meeting(meeting_id):
    """Mark a meeting as finalized with the chosen slot and optional zoom link."""
    data = request.get_json() or {}
    meeting = db_fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    now = datetime.now(timezone.utc).isoformat()
    db_execute(
        "UPDATE meetings SET status = 'finalized', finalized_slot = ?, zoom_link = ?, updated_at = ? WHERE id = ?",
        (data.get("finalized_slot"), data.get("zoom_link"), now, meeting_id),
    )
    db_commit()
    return jsonify({"status": "finalized", "meeting_id": meeting_id})


@app.route("/api/meetings/<meeting_id>", methods=["DELETE"])
def delete_meeting(meeting_id):
    """Permanently delete a meeting and all its data."""
    meeting = db_fetchone("SELECT id FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404
    # CASCADE in schema handles timeslots, participants, votes
    db_execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    db_commit()
    return jsonify({"deleted": meeting_id})


@app.route("/api/meetings/<meeting_id>/remind", methods=["POST"])
def mark_reminded(meeting_id):
    """Mark specific participants as reminded. Body: {"emails": ["a@b.com"]}"""
    data = request.get_json() or {}
    emails = data.get("emails", [])
    now = datetime.now(timezone.utc).isoformat()
    for email in emails:
        db_execute(
            "UPDATE participants SET reminded_at = ? WHERE meeting_id = ? AND email = ?",
            (now, meeting_id, email),
        )
    db_commit()
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

    meeting = db_fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        abort(404, "Meeting not found")
    meeting = row_to_dict(meeting)

    if meeting["status"] != "voting":
        return render_template("vote_closed.html", meeting=meeting)

    participant = db_fetchone(
        "SELECT * FROM participants WHERE meeting_id = ? AND token = ?",
        (meeting_id, token),
    )
    if not participant:
        abort(403, "Invalid voting link")
    participant = row_to_dict(participant)

    timeslots = db_fetchall(
        "SELECT * FROM timeslots WHERE meeting_id = ? ORDER BY start_time",
        (meeting_id,),
    )
    timeslots = [row_to_dict(ts) for ts in timeslots]

    # Check for existing votes
    existing_votes = {}
    if participant["has_voted"]:
        votes = db_fetchall(
            "SELECT timeslot_id, available FROM votes WHERE participant_id = ?",
            (participant["id"],),
        )
        existing_votes = {row_to_dict(v)["timeslot_id"]: row_to_dict(v)["available"] for v in votes}

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

    meeting = db_fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        return jsonify({"error": "Voting is closed"}), 400
    meeting = row_to_dict(meeting)
    if meeting["status"] != "voting":
        return jsonify({"error": "Voting is closed"}), 400

    participant = db_fetchone(
        "SELECT * FROM participants WHERE meeting_id = ? AND token = ?",
        (meeting_id, token),
    )
    if not participant:
        return jsonify({"error": "Invalid token"}), 403
    participant = row_to_dict(participant)

    timeslots = db_fetchall(
        "SELECT id FROM timeslots WHERE meeting_id = ?", (meeting_id,)
    )
    timeslots = [row_to_dict(ts) for ts in timeslots]

    # Delete any existing votes for re-submission
    db_execute("DELETE FROM votes WHERE participant_id = ?", (participant["id"],))

    # Insert new votes
    selected_ids = request.form.getlist("slots")
    for ts in timeslots:
        vote_id = str(uuid.uuid4())
        available = 1 if ts["id"] in selected_ids else 0
        db_execute(
            "INSERT INTO votes (id, participant_id, timeslot_id, available) VALUES (?, ?, ?, ?)",
            (vote_id, participant["id"], ts["id"], available),
        )

    db_execute(
        "UPDATE participants SET has_voted = 1 WHERE id = ?", (participant["id"],)
    )
    db_commit()

    return render_template("vote_thanks.html", meeting=meeting, participant=participant)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": "postgresql" if USE_POSTGRES else "sqlite",
    })


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
