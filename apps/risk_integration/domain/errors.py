class PermanentApplyError(Exception):
    """Applying a decision in the monolith cannot succeed on a retry: a monolith invariant is broken.
    Raised by the monolith adapter; leads to compensation and `CANCEL_PENDING(APPLY_FAILED)`"""
