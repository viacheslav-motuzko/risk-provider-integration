class PermanentApplyError(Exception):
    """Applying a decision in the monolith cannot succeed on a retry: a monolith invariant is broken (for example
    the held stake is missing). Raised by the monolith adapter; leads to compensation and
    `CANCEL_PENDING(APPLY_FAILED)` (ticket-lifecycle row 8)."""
