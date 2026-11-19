"""Cross-cutting security seams shared by the whole fleet.

Home for the *mechanisms* that close a defect class rather than one
occurrence of it: settings the boot checks read, and the small primitives
several modules would otherwise each hand-roll — the client IP of a request
(:func:`client_ip`) and the rate-string parser (:func:`parse_rate`).
"""
from .conf import security_settings
from .proxy import client_ip, trusted_proxy_header
from .ratelimit import InvalidRate, parse_rate

__all__ = [
    "InvalidRate",
    "client_ip",
    "parse_rate",
    "security_settings",
    "trusted_proxy_header",
]
