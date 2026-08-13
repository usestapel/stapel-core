"""
Email-keyed Django authentication backend for cross-service deployments.

Services in a split deployment do not agree on the User primary-key type
(one issues UUIDs, another AutoFields), so a session or a credential that
crosses a service boundary cannot be keyed on the id. Email is the stable
identifier, and that — and only that — is what this backend changes about
``ModelBackend``: the *lookup* key. Credential verification is identical to
Django's, because a Django authentication backend that resolves a principal
without verifying a secret is a full authentication bypass for every caller
of ``django.contrib.auth.authenticate()``, including the legacy token
endpoint in ``stapel-auth``.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

logger = logging.getLogger(__name__)


class EmailAuthBackend(ModelBackend):
    """Authenticate by email + password.

    ``verifies_credentials`` is the explicit declaration the
    ``stapel_core.security`` system check requires of every backend wired
    into ``AUTHENTICATION_BACKENDS``: overriding ``authenticate()`` is the
    moment a project can silently drop the password check, so the contract
    is stated on the class rather than inferred from the body.
    """

    verifies_credentials = True

    def authenticate(self, request, username=None, password=None, **kwargs):
        """Resolve the user by email, then verify the password.

        Deliberately mirrors ``ModelBackend.authenticate`` step for step:

        * a missing/empty password denies — ``authenticate(request,
          email=...)`` with no secret must never return a principal;
        * an unknown email still runs one password hash, so response time
          does not disclose which addresses exist;
        * ``user_can_authenticate()`` denies inactive accounts;
        * an ambiguous email (more than one row — possible wherever the
          user model has no unique constraint on it) denies, because there
          is no single principal the credential could belong to.
        """
        user_model = get_user_model()
        email = kwargs.get("email", username)
        if not email or not password:
            return None

        try:
            user = user_model.objects.get(email=email)
        except user_model.DoesNotExist:
            # Same timing-equalisation ModelBackend does: run the hasher
            # anyway so a nonexistent email is not distinguishable from a
            # wrong password by how long the answer takes.
            user_model().set_password(password)
            return None
        except user_model.MultipleObjectsReturned:
            logger.warning(
                "EmailAuthBackend: %s matches multiple users; denying because "
                "the credential cannot be attributed to one principal",
                email,
            )
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def get_user(self, user_id):
        """Return the user for a session key, or None.

        A session minted by a sibling service can carry a primary key this
        service's User model cannot even parse (UUID text into an integer
        column), which raises rather than returning None. Swallowing that
        into None is what keeps the request anonymous and lets the JWT
        middleware re-authenticate, instead of 500-ing the request.
        """
        user_model = get_user_model()
        try:
            return user_model.objects.get(pk=user_id)
        except (user_model.DoesNotExist, ValueError, TypeError) as exc:
            logger.debug("Session user %r not resolvable: %s", user_id, exc)
            return None
