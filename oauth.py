"""OAuth provider abstraction — framework-agnostic base classes and registry.

Third-party code can register custom providers without modifying stapel-auth:

    # In your app's AppConfig.ready():
    from stapel_core.oauth import register_provider
    from my_app.providers import MyProvider
    register_provider(MyProvider())
"""
import inspect
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class OAuthUserData:
    """Normalized user profile returned by any OAuth provider.

    Attributes:
        id: Provider-specific user ID. Example: 12345
        email: User email if available. Example: user@example.com
        username: Suggested username. Example: johndoe
        avatar: Avatar URL. Example: https://example.com/avatar.jpg
        email_verified: True only when the PROVIDER asserts the email is
            verified. Account merging by email must require this flag —
            merging on an unverified address is an account-takeover vector.
    """
    id: str
    email: str | None
    username: str | None
    avatar: str | None
    email_verified: bool = False


@dataclass(frozen=True)
class OAuthClientConfig:
    """What a provider needs to know about the deployment's own OAuth client.

    Attributes:
        client_id: This deployment's client ID with the provider.
        client_secret: Its secret. Several providers can only introspect a
            token by authenticating AS the app the token was issued to, so a
            verifier needs more than the public id.
        accepted_audiences: Every OAuth client ID a caller-supplied token may
            legitimately have been issued to. A tuple rather than one value
            because one project routinely owns several clients — Google
            issues separate Web / iOS / Android client IDs, so a mobile
            app's token carries a different audience than the web app's and
            both are the same deployment.
    """

    client_id: str = ""
    client_secret: str = ""
    accepted_audiences: tuple = ()


#: The provider has no way to prove which OAuth client minted a token.
AUDIENCE_UNVERIFIABLE = "audience_unverifiable"
#: It does, and the answer is not one of ``accepted_audiences``.
AUDIENCE_MISMATCH = "audience_mismatch"
#: The deployment named no audiences, so there is nothing to compare against.
AUDIENCE_UNPINNED = "audience_unpinned"


class OAuthProvider(ABC):
    """Abstract OAuth 2.0 provider.

    Subclass this and implement ``get_user_data``. Override
    ``get_authorization_url`` and ``exchange_code`` if the provider
    deviates from the standard Authorization Code flow.
    """

    id: str
    display_name: str
    auth_url: str
    token_url: str
    scope: str
    extra_params: dict

    def get_authorization_url(self, client_id: str, redirect_uri: str, state: str) -> str:
        """Build the provider authorization URL."""
        from urllib.parse import urlencode
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": self.scope,
            "state": state,
            "response_type": "code",
            **self.extra_params,
        }
        return self.auth_url + "?" + urlencode(params)

    def exchange_code(
        self, client_id: str, client_secret: str, code: str, redirect_uri: str
    ) -> str | None:
        """Exchange authorization code for access token. Returns token string or None."""
        import requests
        response = requests.post(
            self.token_url,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        return response.json().get("access_token")

    #: Whether :meth:`verify_audience` can actually prove which OAuth client
    #: a bearer access token was issued to. **False by default, and that is
    #: the safe answer**: a provider that has not implemented a verification
    #: mechanism must refuse a caller-supplied token, not quietly pass it.
    #: Flipping this to True without a working ``verify_audience`` is the one
    #: way to make this seam lie.
    verifies_audience: bool = False

    def verify_audience(self, access_token: str, config: OAuthClientConfig) -> bool:
        """Whether *access_token* was minted for one of the accepted clients.

        An OAuth access token is a bearer credential scoped to the client it
        was issued to — it is not, on its own, a statement about who the
        holder is to US. A deployment that accepts a token straight from a
        request body and looks up the profile behind it will happily accept
        a token minted for somebody ELSE'S app against the victim's provider
        account, and log the caller in as the victim. Verifying the audience
        is what closes that door.

        The default refuses. Override it only with a real mechanism (the
        provider's introspection/tokeninfo endpoint) and set
        :attr:`verifies_audience`.
        """
        return False

    @abstractmethod
    def get_user_data(
        self, access_token: str, config: OAuthClientConfig | None = None
    ) -> OAuthUserData | None:
        """Fetch and normalize user profile using the given access token.

        *config* carries this deployment's client credentials for providers
        whose profile call needs them. It is optional: implementations
        written against the pre-0.42 one-argument signature keep working —
        :func:`fetch_user_data` passes it only to implementations that
        declare it.
        """
        ...


_registry: dict[str, OAuthProvider] = {}


def register_provider(provider: OAuthProvider) -> None:
    """Register an OAuth provider globally.

    Call this from your ``AppConfig.ready()`` to make the provider available
    to the auth service without modifying stapel-auth.
    """
    _registry[provider.id] = provider
    logger.debug("OAuth provider registered: %s", provider.id)


def get_provider(provider_id: str) -> OAuthProvider | None:
    """Return a registered provider by ID, or None if not found."""
    return _registry.get(provider_id)


def get_all_providers() -> list[OAuthProvider]:
    """Return all registered providers."""
    return list(_registry.values())


# ── Calling a provider safely ────────────────────────────────────────────────

#: provider class -> whether its get_user_data accepts a *config*. Cached
#: because the answer is a property of the class, and this sits on the login
#: hot path.
_ACCEPTS_CONFIG: dict = {}


def _accepts_config(provider: OAuthProvider) -> bool:
    """Whether *provider*'s ``get_user_data`` takes a config argument.

    Providers registered by third-party apps were written against the
    pre-0.42 ``get_user_data(self, access_token)`` signature. Calling those
    with a second argument is a TypeError at login time, so the seam asks
    before it passes.
    """
    cls = type(provider)
    cached = _ACCEPTS_CONFIG.get(cls)
    if cached is None:
        try:
            params = inspect.signature(cls.get_user_data).parameters
        except (TypeError, ValueError):  # builtins, C wrappers, exotic callables
            cached = False
        else:
            cached = "config" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        _ACCEPTS_CONFIG[cls] = cached
    return cached


def fetch_user_data(
    provider: OAuthProvider,
    access_token: str,
    config: OAuthClientConfig | None = None,
) -> OAuthUserData | None:
    """Call ``provider.get_user_data``, passing *config* only if it takes one."""
    if _accepts_config(provider):
        return provider.get_user_data(access_token, config=config)
    return provider.get_user_data(access_token)


def check_audience(
    provider: OAuthProvider,
    access_token: str,
    config: OAuthClientConfig | None,
) -> str | None:
    """``None`` when the token's audience is accepted; a reason code otherwise.

    Reason codes are :data:`AUDIENCE_UNVERIFIABLE`, :data:`AUDIENCE_MISMATCH`
    and :data:`AUDIENCE_UNPINNED` — all three mean refuse. Callers only pass
    a token they did NOT mint themselves: a token obtained through this
    deployment's own authorization-code exchange is ours by construction and
    needs no check.

    Every failure path, including an exception inside a provider's verifier,
    resolves to a refusal. A verification step that fails open is not a
    verification step.
    """
    if not getattr(provider, "verifies_audience", False):
        # Nothing this provider can do — say so before complaining about
        # configuration the deployment could not have used anyway.
        return AUDIENCE_UNVERIFIABLE
    if config is None or not config.accepted_audiences:
        return AUDIENCE_UNPINNED
    try:
        verified = provider.verify_audience(access_token, config)
    except Exception:
        logger.exception(
            "OAuth audience verification raised for provider %r; refusing",
            getattr(provider, "id", provider),
        )
        return AUDIENCE_UNVERIFIABLE
    return None if verified else AUDIENCE_MISMATCH


__all__ = [
    "AUDIENCE_MISMATCH",
    "AUDIENCE_UNPINNED",
    "AUDIENCE_UNVERIFIABLE",
    "OAuthClientConfig",
    "OAuthProvider",
    "OAuthUserData",
    "check_audience",
    "fetch_user_data",
    "get_all_providers",
    "get_provider",
    "register_provider",
]
