"""Intentional errors raised by the quest application layer."""


class QuestError(Exception):
    """Base class for expected, user-facing quest failures."""


class UsageError(QuestError):
    """A command's arguments do not match its contract."""


class UnknownUser(QuestError):
    """The Telegram user has not been enrolled."""


class InactiveUser(QuestError):
    """The Telegram user was enrolled and later deactivated."""


class NotAuthorized(QuestError):
    """The actor does not have the required role."""


class InvalidQuestState(QuestError):
    """The requested operation is not valid at the captain's position."""


class QuestNotReady(QuestError):
    """Required quest content has not been configured."""


class ContentValidationError(QuestError):
    """Configured content violates a domain invariant."""


class NotFound(QuestError):
    """A requested quest entity does not exist."""
