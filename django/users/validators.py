"""Username validators for the Stapel user canon.

Org-provisioned logins (Workspaces Org Program, spec C1) are namespaced as
``org_slug/local`` — the workspace slug, ONE literal ``/`` separator, then a
local username. Django's stock ``UnicodeUsernameValidator`` forbids ``/``
entirely, so the Stapel user carries this validator instead: it accepts the
exact stock alphabet (letters, digits, ``@ . + - _``), optionally split by a
single slash. Bare usernames validate exactly as before; a leading, trailing
or doubled slash is rejected (both sides of the separator must themselves be
valid usernames).
"""
from __future__ import annotations

from django.core import validators
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _


@deconstructible
class StapelUsernameValidator(validators.RegexValidator):
    """Stock username canon, plus at most one ``/`` as an org-namespace separator."""

    # Each side of the optional slash is the stock UnicodeUsernameValidator
    # alphabet ([\w.@+-]+); "exactly one slash" and "no empty sides" both
    # fall out of the shape <part>(/<part>)?.
    regex = r"^[\w.@+-]+(?:/[\w.@+-]+)?\Z"
    message = _(
        "Enter a valid username. This value may contain only letters, "
        "numbers, and @/./+/-/_ characters, with at most one '/' as an "
        "organization separator."
    )
    flags = 0


__all__ = ["StapelUsernameValidator"]
