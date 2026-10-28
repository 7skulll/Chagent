class SessionNotFoundError(ValueError):
    pass


class SessionArchivedError(ValueError):
    pass


class SessionBusyError(RuntimeError):
    pass


class StateConflictError(RuntimeError):
    pass
