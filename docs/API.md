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
- `GET /api/reservations/{id}` to retrieve the current status and details
- `PATCH /api/reservations/{id}` to change `start`, `end`, `party_size`, or
  `table_id`
- `DELETE /api/reservations/{id}` to cancel

Reservation lookup, change, and cancellation require an `X-Reservation-Email`
header matching the reservation email.

## Manager

Manager reads and writes require an `X-Manager-Token` header. The demo token is
`demo-manager-token`; configure `TABLEKEEPER_MANAGER_TOKEN` in deployments.

- `GET /api/manager/tables`
- `GET /api/manager/reservations`
- `GET /api/manager/closures`
- `GET /api/manager/proposals` and `GET /api/manager/proposals/{id}`
- `GET /api/manager/forecast` for read-only near-term operational risk analysis
- `POST /api/manager/closures` with `table_id`, `start`, `end`, and `reason`
- `POST /api/manager/proposals` with `closure_id`
- `POST /api/manager/proposals/{id}/approve`
- `POST /api/manager/proposals/{id}/reject`

Proposal creation is advisory and does not mutate reservations. Approval
re-checks every alternative in one SQLite transaction; if any alternative is
stale, no reservation is changed.
Managers can reject a draft proposal explicitly; rejection never changes
reservations.

Smart Forecast reviews confirmed reservations in the next seven days and
reports potential table pressure, party-capacity risks, and upcoming closure
impacts. It reuses availability checks to suggest an alternative when one is
safe, but it never changes reservations or proposals.

Closures are bounded windows. They affect availability only between `start` and
`end`; creating one does not permanently change a table's status. A naive
timestamp is interpreted in the restaurant's configured IANA timezone (the
demo restaurant uses `Europe/London`), while an explicit offset is respected
and normalized to UTC. Windows must be positive and no longer than 12 hours.

Mutating requests should provide an `Idempotency-Key`. A retry with the same
key, operation, and canonical payload replays the original response. Reusing a
key for another operation or payload returns `409` rather than replaying the
wrong result. Validation failures use a JSON `{"error": "..."}` response.
Reservation, closure, proposal, approval, and rejection changes are recorded in
`audit_events`.
