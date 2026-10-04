# Tablekeeper API

Run `python3 app.py`, then open `http://localhost:8000/` or
`http://localhost:8000/manager.html`.

Reservation times accept ISO-8601 values. Naive values use the restaurant
timezone; values with an offset are normalized to UTC. Send an
`Idempotency-Key` header on mutating requests and reuse it for retries.

## Customer

- `GET /api/restaurants/demo`
- `GET /api/availability?start=...&end=...&party_size=2`
- `POST /api/reservations` with `customer_name`, `customer_email`,
  `party_size`, `start`, `end`, and optional `table_id`
- `PATCH /api/reservations/{id}` to change `start`, `end`, `party_size`, or
  `table_id`
- `DELETE /api/reservations/{id}` to cancel

## Manager

- `GET /api/manager/tables`
- `GET /api/manager/reservations`
- `GET /api/manager/closures`
- `POST /api/manager/closures` with `table_id`, `start`, `end`, and `reason`
- `POST /api/manager/proposals` with `closure_id`
- `POST /api/manager/proposals/{id}/approve`

Proposal creation is advisory and does not mutate reservations. Approval
re-checks every alternative in one SQLite transaction; if any alternative is
stale, no reservation is changed.
