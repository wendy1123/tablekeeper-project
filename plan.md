# Tablekeeper development plan

## Existing state

The repository contains a dependency-free Python standard-library backend in
`app.py`, draft architecture/plan files, and a modified README. The first
implementation pass is extending that backend rather than replacing it, while
adding the missing browser surfaces, cancellation endpoint, API reference, and
automated tests.

## Delivery phases

1. **Foundation** — add a SQLite schema, seed restaurant/table data, UTC/time-zone helpers, JSON API, and static customer/manager pages.
2. **Reservation safety** — implement availability, create/change/cancel, idempotency, serialized writes, overlap checks, and tests for concurrent-style retries.
3. **Operations workflow** — let managers close tables, identify impacted reservations, generate assistant alternatives, and require explicit approval.
4. **Atomic application and docs** — re-check proposed alternatives inside one transaction, update reservations atomically, expose audit events, and document the API and run commands.
5. **Validation** — run the complete standard-library test suite plus a compile check.

## Acceptance criteria

- Customers can view tables, availability, and reservation status.
- Reservation writes are timezone-aware, overlap-safe, and retry-safe.
- Managers can close tables and see affected reservations.
- The assistant produces explainable alternatives without mutating reservations.
- Only an approved proposal can mutate reservations, and a conflict rolls back every change.
- Documentation and automated tests cover the safety-critical behavior.

```arch
{
  "kind": "layered",
  "title": "Tablekeeper reservation and operations flow",
  "layers": [
    {
      "id": "customer_ui",
      "title": "Customer UI",
      "items": [
        { "id": "customer_page", "label": "Customer reservation page" }
      ]
    },
    {
      "id": "manager_ui",
      "title": "Manager UI",
      "items": [
        { "id": "manager_page", "label": "Manager operations page" }
      ]
    },
    {
      "id": "api",
      "title": "Application API",
      "items": [
        { "id": "reservation_api", "label": "Reservation and availability API" },
        { "id": "operations_api", "label": "Closure, proposal, and approval API" }
      ]
    },
    {
      "id": "storage",
      "title": "SQLite storage",
      "items": [
        { "id": "reservation_store", "label": "Reservations and idempotency records" },
        { "id": "operations_store", "label": "Tables, closures, proposals, and audit events" }
      ]
    }
  ],
  "flows": [
    { "from": "customer_ui", "to": "api", "label": "JSON over HTTP" },
    { "from": "manager_ui", "to": "api", "label": "JSON over HTTP" },
    { "from": "api", "to": "storage", "label": "Transactional SQL" }
  ]
}
```
