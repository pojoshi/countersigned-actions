class GateError(Exception):
    """Base class. `status` is the HTTP status the API maps it to."""

    status = 400


class NotFound(GateError):
    status = 404


class Conflict(GateError):
    status = 409


class Forbidden(GateError):
    status = 403


class Unauthorized(GateError):
    status = 401


class Expired(GateError):
    status = 410


class EffectBlocked(Exception):
    """Raised by an effect when the world no longer matches what was decided."""
