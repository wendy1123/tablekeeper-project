import os
import tempfile
import unittest
import http.client
import json
import threading

import app


class TablekeeperTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(delete=False)
        self.database.close()
        self.old_path = app.DB_PATH
        app.DB_PATH = self.database.name
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.old_path
        os.unlink(self.database.name)

    def test_timezone_normalization(self):
        start, end = app.validate_window("2026-10-01T19:00:00+02:00", "2026-10-01T20:00:00+02:00")
        self.assertEqual(start, "2026-10-01T17:00:00+00:00")
        self.assertEqual(end, "2026-10-01T18:00:00+00:00")

    def test_naive_time_uses_restaurant_timezone(self):
        start, end = app.validate_window(
            "2026-10-25T01:30:00", "2026-10-25T02:30:00", "Europe/London"
        )
        self.assertEqual(start, "2026-10-25T00:30:00+00:00")
        self.assertEqual(end, "2026-10-25T02:30:00+00:00")

    def test_overlapping_reservation_is_not_available(self):
        db = app.connect()
        start = "2026-10-01T17:00:00+00:00"
        end = "2026-10-01T18:00:00+00:00"
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            ("r1", "demo", "t1", "A", "a@example.test", 2, start, end, start, start),
        )
        db.commit()
        tables = app.available_tables(db, "demo", start, end, 2)
        self.assertNotIn("t1", [table["id"] for table in tables])
        db.close()

    def test_existing_reservation_can_change_without_self_conflict(self):
        db = app.connect()
        start = "2026-10-01T17:00:00+00:00"
        end = "2026-10-01T18:00:00+00:00"
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            ("r1", "demo", "t1", "A", "a@example.test", 2, start, end, start, start),
        )
        db.commit()
        tables = app.available_tables(db, "demo", start, end, 2, exclude_reservation_id="r1")
        self.assertIn("t1", [table["id"] for table in tables])
        db.close()

    def test_change_rejects_unavailable_requested_table(self):
        db = app.connect()
        start = "2026-10-01T17:00:00+00:00"
        end = "2026-10-01T18:00:00+00:00"
        db.execute("BEGIN IMMEDIATE")
        for reservation_id, table_id in (("r1", "t1"), ("r2", "t2")):
            db.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                (reservation_id, "demo", table_id, "A", "a@example.test", 2, start, end, start, start),
            )
        db.commit()
        handler = object.__new__(app.Handler)
        with self.assertRaises(app.APIError):
            handler.change_reservation(
                db, "r1",
                {"start": start, "end": end, "party_size": 2, "table_id": "t2"}, None,
            )
        db.close()

    def test_idempotency_key_is_bound_to_payload(self):
        db = app.connect()
        handler = object.__new__(app.Handler)
        handler.send_json = lambda payload, status=200: payload
        body = {
            "customer_name": "A", "customer_email": "a@example.test",
            "party_size": 2, "start": "2026-10-01T17:00:00+00:00",
            "end": "2026-10-01T18:00:00+00:00",
        }
        handler.create_reservation(db, body, "same-key")
        with self.assertRaises(app.APIError):
            handler.create_reservation(db, {**body, "customer_name": "B"}, "same-key")
        db.close()

    def test_cancel_is_terminal(self):
        db = app.connect()
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            ("r1", "demo", "t1", "A", "a@example.test", 2, "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", app.now(), app.now()),
        )
        db.commit()
        handler = object.__new__(app.Handler)
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        handler.cancel_reservation(db, "r1", None)
        self.assertEqual(captured["payload"]["reservation"]["status"], "cancelled")
        db.close()

    def test_proposal_rejects_two_items_on_same_table(self):
        db = app.connect()
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO closures VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c1", "demo", "t1", "test", "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", app.now()),
        )
        for reservation_id in ("r1", "r2"):
            db.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                (reservation_id, "demo", "t1", "A", "a@example.test", 2, "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", app.now(), app.now()),
            )
        db.execute(
            "INSERT INTO proposals VALUES (?, ?, ?, 'draft', ?, NULL)",
            ("p1", "demo", "c1", app.now()),
        )
        for reservation_id, item_id in (("r1", "i1"), ("r2", "i2")):
            db.execute(
                "INSERT INTO proposal_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, "p1", reservation_id, "t2", "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", "test"),
            )
        db.commit()
        handler = object.__new__(app.Handler)
        with self.assertRaises(app.APIError):
            handler.approve_proposal(db, "p1", None)
        self.assertEqual(db.execute("SELECT status FROM proposals WHERE id='p1'").fetchone()["status"], "draft")
        db.rollback()
        db.close()

    def test_proposal_rejects_unavailable_target_table(self):
        db = app.connect()
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO closures VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c1", "demo", "t1", "test", "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", app.now()),
        )
        db.execute(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            ("r1", "demo", "t1", "A", "a@example.test", 2, "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", app.now(), app.now()),
        )
        db.execute("UPDATE restaurant_tables SET status='maintenance' WHERE id='t2'")
        db.execute(
            "INSERT INTO proposals VALUES (?, ?, ?, 'draft', ?, NULL)",
            ("p1", "demo", "c1", app.now()),
        )
        db.execute(
            "INSERT INTO proposal_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("i1", "p1", "r1", "t2", "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", "test"),
        )
        db.commit()
        handler = object.__new__(app.Handler)
        with self.assertRaises(app.APIError):
            handler.approve_proposal(db, "p1", None)
        self.assertEqual(db.execute("SELECT table_id FROM reservations WHERE id='r1'").fetchone()["table_id"], "t1")
        db.close()

    def test_proposal_rejection_is_terminal_and_does_not_move_reservations(self):
        db = app.connect()
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO closures VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c1", "demo", "t1", "test", "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", app.now()),
        )
        db.execute(
            "INSERT INTO proposals VALUES (?, ?, ?, 'draft', ?, NULL)",
            ("p1", "demo", "c1", app.now()),
        )
        db.commit()
        handler = object.__new__(app.Handler)
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        handler.reject_proposal(db, "p1", "reject-key")
        self.assertEqual(captured["payload"]["status"], "rejected")
        self.assertEqual(
            db.execute("SELECT status FROM proposals WHERE id='p1'").fetchone()["status"],
            "rejected",
        )
        with self.assertRaises(app.APIError):
            handler.reject_proposal(db, "p1", "reject-again")
        db.close()

    def test_two_connections_serialize_competing_reservations(self):
        body = {
            "customer_name": "A",
            "customer_email": "a@example.test",
            "party_size": 6,
            "start": "2026-10-01T17:00:00+00:00",
            "end": "2026-10-01T18:00:00+00:00",
            "table_id": "t3",
        }
        results = []
        barrier = threading.Barrier(2)

        def attempt(email):
            db = app.connect()
            handler = object.__new__(app.Handler)
            handler.send_json = lambda payload, status=200: payload
            barrier.wait()
            try:
                result = handler.create_reservation(
                    db, {**body, "customer_email": email}, None
                )
                results.append(("ok", result["reservation"]["id"]))
            except app.APIError as exc:
                results.append(("error", exc.status))
            finally:
                db.close()

        threads = [
            threading.Thread(target=attempt, args=(f"{letter}@example.test",))
            for letter in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([result[0] for result in results].count("ok"), 1)
        self.assertEqual([result[0] for result in results].count("error"), 1)

    def test_http_reservation_lifecycle_and_cors(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection(*server.server_address)
            payload = json.dumps(body) if body is not None else None
            connection.request(method, path, payload, headers or {})
            response = connection.getresponse()
            data = json.loads(response.read() or b"{}")
            connection.close()
            return response.status, response.headers, data

        try:
            status, headers, _ = request("OPTIONS", "/api/reservations/r1")
            self.assertEqual(status, 204)
            self.assertIn("DELETE", headers["Access-Control-Allow-Methods"])

            body = {
                "customer_name": "A",
                "customer_email": "owner@example.test",
                "party_size": 2,
                "start": "2026-10-01T17:00:00+00:00",
                "end": "2026-10-01T18:00:00+00:00",
            }
            status, _, created = request(
                "POST",
                "/api/reservations",
                body,
                {"Content-Type": "application/json", "Idempotency-Key": "http-create"},
            )
            self.assertEqual(status, 201)
            reservation_id = created["reservation"]["id"]

            status, _, _ = request("GET", f"/api/reservations/{reservation_id}")
            self.assertEqual(status, 403)
            owner_headers = {"X-Reservation-Email": "owner@example.test"}
            status, _, looked_up = request("GET", f"/api/reservations/{reservation_id}", headers=owner_headers)
            self.assertEqual(status, 200)
            self.assertEqual(looked_up["reservation"]["id"], reservation_id)

            change = {
                "start": "2026-10-01T19:00:00+00:00",
                "end": "2026-10-01T20:00:00+00:00",
                "party_size": 2,
            }
            status, _, changed = request(
                "PATCH",
                f"/api/reservations/{reservation_id}",
                change,
                {**owner_headers, "Content-Type": "application/json", "Idempotency-Key": "http-change"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(changed["reservation"]["start"], change["start"].replace("+00:00", "Z"))

            status, _, cancelled = request(
                "DELETE",
                f"/api/reservations/{reservation_id}",
                headers={**owner_headers, "Idempotency-Key": "http-cancel"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(cancelled["reservation"]["status"], "cancelled")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_non_object_json_body_is_rejected(self):
        self.assertRaises(app.APIError, app.Handler.read_json, type("Request", (), {
            "headers": {"Content-Length": "2"},
            "rfile": type("Body", (), {"read": lambda self, length: b"[]"} )(),
        })())

    def test_proposal_detail_is_scoped_to_restaurant(self):
        db = app.connect()
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO restaurants VALUES (?, ?, ?)", ("other", "Other", "UTC"))
        db.execute("INSERT INTO restaurant_tables VALUES (?, ?, ?, ?, 'available')", ("other-t1", "other", "Other table", 2))
        db.execute(
            "INSERT INTO closures VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c-other", "other", "other-t1", "test", "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00", app.now()),
        )
        db.execute("INSERT INTO proposals VALUES (?, ?, ?, 'draft', ?, NULL)", ("p-other", "other", "c-other", app.now()))
        db.commit()
        proposal = db.execute(
            "SELECT * FROM proposals WHERE id=? AND restaurant_id=?",
            ("p-other", "demo"),
        ).fetchone()
        self.assertIsNone(proposal)
        db.close()


if __name__ == "__main__":
    unittest.main()
