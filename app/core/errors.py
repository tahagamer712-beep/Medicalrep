"""Domain exceptions. Services raise these; interfaces translate them."""


class DomainError(Exception):
    """Base class for all business-logic errors."""


class NotFound(DomainError):
    """Requested record does not exist (in the caller's permitted scope)."""


class PermissionDenied(DomainError):
    """Actor lacks permission for the requested action/scope."""


class ValidationError(DomainError):
    """Input data failed business validation."""


class Conflict(DomainError):
    """Operation conflicts with existing state (e.g. duplicate)."""


class AlreadyExists(Conflict):
    """A record with the same unique key already exists."""
