"""Pilot presenter for the core User model (§55 slice 1 proof).

Exercises the whole slice on one real core model: an as-is + custom-field
presenter over the ``users.User`` DAO, OpenAPI schema generation through
:class:`~stapel_core.django.api.serializers.StapelDataclassSerializer`, and
config-swap of the presenter through :mod:`stapel_core.django.swappable` —
no fork of ``stapel_core.django.users`` required for a host to change either.

A host project replaces the presenter with its own subclass via
``STAPEL_SWAP``::

    STAPEL_SWAP = {
        "USERS_PROFILE_PRESENTER": "myapp.presenters.HostUserProfilePresenter",
    }

and consumes it through :func:`get_user_profile_presenter` rather than
importing :class:`UserProfilePresenter` directly (the point of the
indirection — see :mod:`stapel_core.django.swappable`).
"""
from __future__ import annotations

from stapel_core.django.api.presenters import Presenter, PresenterField
from stapel_core.django.swappable import get_presenter

#: Swap key for the host presenter override (STAPEL_SWAP registry).
PRESENTER_KEY = "USERS_PROFILE_PRESENTER"


def _user_model():
    # Deliberately Django's own AUTH_USER_MODEL indirection, not
    # stapel_core.django.swappable.get_model(): the concrete User class is
    # already swappable by Django's native mechanism (settings.AUTH_USER_MODEL
    # / get_user_model()) — every Stapel project already goes through it for
    # the user model specifically, so a second, parallel STAPEL_SWAP entry
    # for the same class would just be a second knob for the same decision.
    from django.contrib.auth import get_user_model

    return get_user_model()


class UserProfilePresenter(Presenter):
    """Presents a User row as the public profile view.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "email": "user@example.com",
            "display_name": "Alice"
        }
    """

    model = _user_model()
    fields = ("id", "email")
    custom_fields = {
        "display_name": PresenterField(
            type=str,
            source=lambda dao: dao.username,
            help_text="Public display name (falls back to the username).",
        ),
    }


def get_user_profile_presenter() -> type[Presenter]:
    """The active (possibly host-swapped) user profile presenter.

    Consumers (views, other presenters nesting this one) call this instead
    of importing :class:`UserProfilePresenter` directly — that direct import
    is exactly what a ``STAPEL_SWAP["USERS_PROFILE_PRESENTER"]`` override
    would silently fail to reach (the SWAP001 lint, next wave, will flag it).
    """
    return get_presenter(
        PRESENTER_KEY,
        default="stapel_core.django.users.presenters.UserProfilePresenter",
    )


__all__ = ["PRESENTER_KEY", "UserProfilePresenter", "get_user_profile_presenter"]
