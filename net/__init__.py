"""Outbound-network primitives shared by every module that fetches a URL."""
from .safe_fetch import SafeFetchError, SafeFetchResult, fetch_bytes

__all__ = ["SafeFetchError", "SafeFetchResult", "fetch_bytes"]
