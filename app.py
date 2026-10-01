"""Dependency-free Tablekeeper HTTP application."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).parent
DB_PATH = os.environ.get("TABLEKEEPER_DB", str(ROOT / "tablekeeper.db"))


def utc_time(value: str, default_zone: str = "UTC") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(default_zone))
        return parsed.astimezone(timezone.utc)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("time must be ISO-8601 with a valid IANA timezone") from exc


def iso(value: str) -> str:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS restaurants (id TEXT PRIMARY KEY, name TEXT NOT NULL, timezone TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS restaurant_tables (
  id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
  name TEXT NOT NULL, capacity INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'available'
);
CREATE TABLE IF NOT EXISTS reservations (
  id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
  table_id TEXT NOT NULL REFERENCES restaurant_tables(id), customer_name TEXT NOT NULL,
  customer_email TEXT NOT NULL, party_size INTEGER NOT NULL, start_utc TEXT NOT NULL,
  end_utc TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON reservations(table_id, start_utc, end_utc, status);
CREATE TABLE IF NOT EXISTS closures (
  id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
  table_id TEXT NOT NULL REFERENCES restaurant_tables(id), reason TEXT NOT NULL,
  start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
  closure_id TEXT NOT NULL REFERENCES closures(id), status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL, approved_at TEXT
);
CREATE TABLE IF NOT EXISTS proposal_items (
  id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
  reservation_id TEXT NOT NULL REFERENCES reservations(id), new_table_id TEXT NOT NULL,
  new_start_utc TEXT NOT NULL, new_end_utc TEXT NOT NULL, rationale TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY, operation TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def init_db(connection: sqlite3.Connection | None = None) -> None:
    own = connection is None
    connection = connection or connect()
    connection.executescript(SCHEMA)
    if not connection.execute("SELECT 1 FROM restaurants LIMIT 1").fetchone():
        connection.execute("INSERT INTO restaurants VALUES (?, ?, ?)", ("demo", "The Tablekeeper Kitchen", "Europe/London"))
        for table_id, name, capacity in (("t1", "Window 1", 2), ("t2", "Window 2", 4), ("t3", "Garden 1", 6), ("t4", "Chef's Counter", 4)):
            connection.execute("INSERT INTO restaurant_tables VALUES (?, ?, ?, ?, 'available')", (table_id, "demo", name, capacity))
    if own:
        connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def idempotent(connection, key: str | None, operation: str, action):
    if key:
        prior = connection.execute("SELECT response_json FROM idempotency WHERE key=?", (key,)).fetchone()
        if prior:
            return json.loads(prior["response_json"])
    result = action()
    if key:
        connection.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?)", (key, operation, json.dumps(result), now()))
    return result


def validate_window(start: str, end: str) -> tuple[str, str]:
    start_dt, end_dt = utc_time(start), utc_time(end)
    if end_dt <= start_dt or end_dt - start_dt > timedelta(hours=12):
        raise ValueError("end must be after start and the booking may not exceed 12 hours")
    return start_dt.isoformat(), end_dt.isoformat()


def available_tables(connection, restaurant_id, start_utc, end_utc, party_size, exclude_reservation_id=None):
    return connection.execute(
        """SELECT t.* FROM restaurant_tables t
        WHERE t.restaurant_id=? AND t.capacity>=? AND t.status='available'
        AND NOT EXISTS (SELECT 1 FROM reservations r WHERE r.table_id=t.id AND r.status='confirmed'
          AND r.id <> COALESCE(?, '') AND r.start_utc < ? AND r.end_utc > ?)
        AND NOT EXISTS (SELECT 1 FROM closures c WHERE c.table_id=t.id
          AND c.start_utc < ? AND c.end_utc > ?)
        ORDER BY t.capacity, t.id""",
        (restaurant_id, party_size, exclude_reservation_id, end_utc, start_utc, end_utc, start_utc),
    ).fetchall()


def reservation_view(row):
    result = row_dict(row)
    if result:
        result["start"] = iso(result.pop("start_utc"))
        result["end"] = iso(result.pop("end_utc"))
    return result


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Handler(BaseHTTPRequestHandler):
    server_version = "Tablekeeper/1.0"

    def log_message(self, *_):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise APIError("request body must be valid JSON") from exc

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.end_headers()

    def do_GET(self):
        connection = connect()
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            if path in ("/", "/index.html", "/manager.html"):
                page = ROOT / ("manager.html" if path == "/manager.html" else "index.html")
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/restaurants/demo":
                restaurant = row_dict(connection.execute("SELECT * FROM restaurants WHERE id='demo'").fetchone())
                restaurant["tables"] = [row_dict(r) for r in connection.execute("SELECT * FROM restaurant_tables WHERE restaurant_id='demo'")]
                return self.send_json(restaurant)
            if path == "/api/availability":
                start, end = validate_window(params["start"], params["end"])
                tables = [row_dict(r) for r in available_tables(connection, params.get("restaurant_id", "demo"), start, end, int(params["party_size"]))]
                return self.send_json({"tables": tables, "start": iso(start), "end": iso(end)})
            if path == "/api/manager/reservations":
                rows = connection.execute("SELECT r.*, t.name table_name FROM reservations r JOIN restaurant_tables t ON t.id=r.table_id WHERE r.restaurant_id=? ORDER BY r.start_utc", (params.get("restaurant_id", "demo"),)).fetchall()
                return self.send_json({"reservations": [reservation_view(r) for r in rows]})
            if path == "/api/manager/closures":
                rows = connection.execute("SELECT c.*, t.name table_name FROM closures c JOIN restaurant_tables t ON t.id=c.table_id ORDER BY c.start_utc DESC").fetchall()
                return self.send_json({"closures": [row_dict(r) for r in rows]})
            if path == "/api/manager/tables":
                rows = connection.execute("SELECT * FROM restaurant_tables WHERE restaurant_id=? ORDER BY id", (params.get("restaurant_id", "demo"),)).fetchall()
                return self.send_json({"tables": [row_dict(r) for r in rows]})
            raise APIError("route not found", 404)
        except (APIError, KeyError, ValueError) as exc:
            self.send_json({"error": getattr(exc, "message", str(exc))}, getattr(exc, "status", 400))
        finally:
            connection.close()

    def do_POST(self):
        connection = connect()
        try:
            path = self.path.split("?", 1)[0]
            body = self.read_json()
            key = self.headers.get("Idempotency-Key")
            if path == "/api/reservations":
                return self.create_reservation(connection, body, key)
            if path == "/api/manager/closures":
                return self.create_closure(connection, body, key)
            if path == "/api/manager/proposals":
                return self.create_proposal(connection, body, key)
            if path.startswith("/api/manager/proposals/") and path.endswith("/approve"):
                return self.approve_proposal(connection, path.split("/")[4], key)
            raise APIError("route not found", 404)
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            self.send_json({"error": "request conflicts with existing data; retry with fresh availability"}, 409)
        except (APIError, KeyError, ValueError) as exc:
            connection.rollback()
            self.send_json({"error": getattr(exc, "message", str(exc))}, getattr(exc, "status", 400))
        finally:
            connection.close()

    def do_PATCH(self):
        connection = connect()
        try:
            path = self.path.split("?", 1)[0]
            body = self.read_json()
            if path.startswith("/api/reservations/"):
                return self.change_reservation(connection, path.rsplit("/", 1)[1], body, self.headers.get("Idempotency-Key"))
            raise APIError("route not found", 404)
        except (APIError, KeyError, ValueError) as exc:
            connection.rollback()
            self.send_json({"error": getattr(exc, "message", str(exc))}, getattr(exc, "status", 400))
        finally:
            connection.close()

    def do_DELETE(self):
        connection = connect()
        try:
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/reservations/"):
                return self.cancel_reservation(connection, path.rsplit("/", 1)[1], self.headers.get("Idempotency-Key"))
            raise APIError("route not found", 404)
        except (APIError, KeyError, ValueError) as exc:
            connection.rollback()
            self.send_json({"error": getattr(exc, "message", str(exc))}, getattr(exc, "status", 400))
        finally:
            connection.close()

    def create_reservation(self, db, body, key):
        def action():
            start, end = validate_window(body["start"], body["end"])
            db.execute("BEGIN IMMEDIATE")
            restaurant_id = body.get("restaurant_id", "demo")
            tables = available_tables(db, restaurant_id, start, end, int(body["party_size"]))
            if body.get("table_id"):
                tables = [table for table in tables if table["id"] == body["table_id"]]
            if not tables:
                raise APIError("no table is available for that time", 409)
            reservation_id = str(uuid.uuid4())
            timestamp = now()
            db.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                       (reservation_id, restaurant_id, tables[0]["id"],
                        body["customer_name"], body["customer_email"], int(body["party_size"]), start, end, timestamp, timestamp))
            db.execute("INSERT INTO audit_events VALUES (?, 'reservation.created', ?, ?, ?)", (str(uuid.uuid4()), reservation_id, json.dumps(body), timestamp))
            db.commit()
            return {"reservation": reservation_view(db.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone())}
        return self.send_json(idempotent(db, key, "reservation.create", action), 201)

    def change_reservation(self, reservation_id, body, key):
        def action():
            start, end = validate_window(body["start"], body["end"])
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM reservations WHERE id=? AND status='confirmed'", (reservation_id,)).fetchone()
            if not current:
                raise APIError("reservation not found", 404)
            tables = available_tables(
                db, current["restaurant_id"], start, end,
                int(body.get("party_size", current["party_size"])),
                exclude_reservation_id=reservation_id,
            )
            tables = [t for t in tables if t["id"] == body.get("table_id", t["id"]) or t["id"] != current["table_id"]]
            if not tables:
                raise APIError("no table is available for that change", 409)
            table_id = body.get("table_id", tables[0]["id"])
            db.execute("UPDATE reservations SET table_id=?, party_size=?, start_utc=?, end_utc=?, updated_at=? WHERE id=?",
                       (table_id, int(body.get("party_size", current["party_size"])), start, end, now(), reservation_id))
            db.commit()
            return {"reservation": reservation_view(db.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone())}
        return self.send_json(idempotent(db, key, "reservation.change", action))

    def cancel_reservation(self, db, reservation_id, key):
        def action():
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM reservations WHERE id=? AND status='confirmed'", (reservation_id,)).fetchone()
            if not current:
                raise APIError("reservation not found or already cancelled", 404)
            timestamp = now()
            db.execute("UPDATE reservations SET status='cancelled', updated_at=? WHERE id=?", (timestamp, reservation_id))
            db.execute("INSERT INTO audit_events VALUES (?, 'reservation.cancelled', ?, ?, ?)",
                       (str(uuid.uuid4()), reservation_id, json.dumps({"reservation_id": reservation_id}), timestamp))
            db.commit()
            return {"reservation": reservation_view(db.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone())}
        return self.send_json(idempotent(db, key, "reservation.cancel", action))

    def create_closure(self, db, body, key):
        def action():
            start, end = validate_window(body["start"], body["end"])
            db.execute("BEGIN IMMEDIATE")
            restaurant_id = body.get("restaurant_id", "demo")
            table = db.execute(
                "SELECT id FROM restaurant_tables WHERE id=? AND restaurant_id=?",
                (body["table_id"], restaurant_id),
            ).fetchone()
            if not table:
                raise APIError("table not found for restaurant", 404)
            closure_id = str(uuid.uuid4())
            db.execute("INSERT INTO closures VALUES (?, ?, ?, ?, ?, ?, ?)", (closure_id, restaurant_id, body["table_id"], body.get("reason", "Maintenance"), start, end, now()))
            affected = db.execute("SELECT r.*, t.name table_name FROM reservations r JOIN restaurant_tables t ON t.id=r.table_id WHERE r.table_id=? AND r.status='confirmed' AND r.start_utc < ? AND r.end_utc > ?", (body["table_id"], end, start)).fetchall()
            db.execute("UPDATE restaurant_tables SET status='closed' WHERE id=?", (body["table_id"],))
            db.commit()
            return {"closure": row_dict(db.execute("SELECT * FROM closures WHERE id=?", (closure_id,)).fetchone()), "affected_reservations": [reservation_view(r) for r in affected]}
        return self.send_json(idempotent(db, key, "closure.create", action), 201)

    def create_proposal(self, db, body, key):
        def action():
            db.execute("BEGIN IMMEDIATE")
            closure = db.execute("SELECT * FROM closures WHERE id=?", (body["closure_id"],)).fetchone()
            if not closure:
                raise APIError("closure not found", 404)
            affected = db.execute("SELECT r.* FROM reservations r WHERE r.table_id=? AND r.status='confirmed' AND r.start_utc < ? AND r.end_utc > ?", (closure["table_id"], closure["end_utc"], closure["start_utc"])).fetchall()
            proposal_id = str(uuid.uuid4())
            db.execute("INSERT INTO proposals VALUES (?, ?, ?, 'draft', ?, NULL)", (proposal_id, closure["restaurant_id"], closure["id"], now()))
            suggestions = []
            for reservation in affected:
                choices = available_tables(db, reservation["restaurant_id"], reservation["start_utc"], reservation["end_utc"], reservation["party_size"])
                if choices:
                    choice = choices[0]
                    rationale = f"Same time; {choice['name']} fits {reservation['party_size']} guests."
                    db.execute("INSERT INTO proposal_items VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), proposal_id, reservation["id"], choice["id"], reservation["start_utc"], reservation["end_utc"], rationale))
                    suggestions.append({"reservation_id": reservation["id"], "new_table_id": choice["id"], "rationale": rationale})
                else:
                    suggestions.append({"reservation_id": reservation["id"], "unresolved": True, "rationale": "No safe table found at the original time."})
            db.commit()
            return {"proposal_id": proposal_id, "status": "draft", "suggestions": suggestions}
        return self.send_json(idempotent(db, key, "proposal.create", action), 201)

    def approve_proposal(self, db, proposal_id, key):
        def action():
            db.execute("BEGIN IMMEDIATE")
            proposal = db.execute("SELECT * FROM proposals WHERE id=? AND status='draft'", (proposal_id,)).fetchone()
            if not proposal:
                raise APIError("draft proposal not found or already processed", 409)
            items = db.execute("SELECT * FROM proposal_items WHERE proposal_id=?", (proposal_id,)).fetchall()
            if not items:
                raise APIError("proposal has no safe alternatives", 409)
            planned = []
            for item in items:
                conflicts = db.execute("SELECT 1 FROM reservations WHERE table_id=? AND status='confirmed' AND id<>? AND start_utc < ? AND end_utc > ?",
                                       (item["new_table_id"], item["reservation_id"], item["new_end_utc"], item["new_start_utc"])).fetchone()
                closed = db.execute("SELECT 1 FROM closures WHERE table_id=? AND start_utc < ? AND end_utc > ?", (item["new_table_id"], item["new_end_utc"], item["new_start_utc"])).fetchone()
                planned_conflict = any(
                    prior["table_id"] == item["new_table_id"]
                    and prior["start_utc"] < item["new_end_utc"]
                    and prior["end_utc"] > item["new_start_utc"]
                    for prior in planned
                )
                if conflicts or closed or planned_conflict:
                    raise APIError("proposal is stale; availability changed, so nothing was applied", 409)
                planned.append({
                    "table_id": item["new_table_id"],
                    "start_utc": item["new_start_utc"],
                    "end_utc": item["new_end_utc"],
                })
            for item in items:
                db.execute("UPDATE reservations SET table_id=?, start_utc=?, end_utc=?, updated_at=? WHERE id=?", (item["new_table_id"], item["new_start_utc"], item["new_end_utc"], now(), item["reservation_id"]))
            db.execute("UPDATE proposals SET status='approved', approved_at=? WHERE id=?", (now(), proposal_id))
            db.commit()
            return {"proposal_id": proposal_id, "status": "approved", "updated_reservations": len(items)}
        return self.send_json(idempotent(db, key, "proposal.approve", action))


def serve(port=8000):
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Tablekeeper listening on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "8000")))
