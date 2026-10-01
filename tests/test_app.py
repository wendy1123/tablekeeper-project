import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
