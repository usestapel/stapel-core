"""Exceptions of the access package."""


class AccessConfigError(Exception):
    """Invalid ``STAPEL_ACCESS`` configuration (bad role/model entry).

    Raised at resolve time and surfaced by the ``stapel_access`` system
    checks as E-level findings — a malformed access policy is a deploy
    blocker, not a runtime fallback.
    """


__all__ = ["AccessConfigError"]
