"""Cross-cutting security seams shared by the whole fleet.

Home for the *mechanisms* that close a defect class rather than one
occurrence of it: settings the boot checks read, and the small primitives
several modules would otherwise each hand-roll.
"""
from .conf import security_settings

__all__ = ["security_settings"]
