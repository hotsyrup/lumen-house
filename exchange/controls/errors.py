"""Stable redacted errors shared by native persistence and recovery fences."""


class RepositoryError(Exception):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status
