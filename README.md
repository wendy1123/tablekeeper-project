# Tablekeeper

Tablekeeper is a full-stack restaurant reservation system for the WeAreDevelopers × BAND Dark Factory hackathon. It combines customer reservations and manager operations with an approval-gated assistant for handling reservations affected by table closures.

## Development

The application uses only Python's standard library:

```bash
python3 -m unittest discover -s tests -v
python3 app.py
```

Open <http://localhost:8000/> for the customer view and <http://localhost:8000/manager.html> for manager operations.

The API stores data in SQLite (`tablekeeper.db` by default). Set `TABLEKEEPER_DB` to use a different database path, including `:memory:` for tests. Manager routes require `X-Manager-Token` (the demo token is `demo-manager-token`; set `TABLEKEEPER_MANAGER_TOKEN` to change it). Customers must send `X-Reservation-Email` when looking up or changing a reservation.

## Safety properties

- Reservation times are accepted with an IANA timezone and normalized to UTC.
- SQLite transactions use `BEGIN IMMEDIATE`; availability is rechecked while the write lock is held, which serializes competing overlap checks.
- Idempotency keys make retried create, change, cancel, closure, proposal, and approval requests safe.
- Closure impact analysis is explicit; assistant proposals are draft-only until a manager approves them.
- Approval re-checks availability and applies all changes atomically, rolling back the complete batch on conflict.
- Closures are time-bounded and do not permanently hide a table; local naive times use the restaurant's IANA timezone.
- Reusing an idempotency key with a different operation or payload is rejected with HTTP 409.

## API overview

Customer endpoints are under `/api/restaurants/{restaurant_id}`, `/api/availability`, and `/api/reservations`. Manager endpoints are under `/api/manager/...`; see `docs/API.md` for request and response examples.
