"""Controller errors shared by in-process and HTTP callers."""
SIMULATED = 'simulated'
UNAVAILABLE = 'unavailable'
LIVE = 'live'


class ServiceError(Exception):
    status = 500
    code = 'service_error'

    def __init__(self, message, *, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class ValidationError(ServiceError):
    status = 400
    code = 'validation_error'


class ConflictError(ServiceError):
    status = 409
    code = 'conflict'


class AdapterError(ServiceError):
    status = 503
    code = 'subsystem_unavailable'
