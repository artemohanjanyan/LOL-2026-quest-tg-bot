class StoreError(RuntimeError):
    """Base class for expected persistence-layer failures."""


class StoreClosedError(StoreError):
    """Raised when an operation is attempted after closing the store."""


class InstanceAlreadyRunningError(StoreError):
    """Raised when another process owns the database instance lock."""


class RecordNotFoundError(StoreError):
    """Raised when an operation targets a missing persistent entity."""


class StateConflictError(StoreError):
    """Raised when the captain is not in the state required by an operation."""


class DuplicateUpdateError(StoreError):
    """Raised when one Telegram update ID is reused for another action."""


class TaskAlreadySolvedError(StateConflictError):
    """Raised when a captain attempts a currently solved task."""


class TaskLimitExceededError(StoreError):
    """Raised when adding a task would exceed the per-stage limit."""
