"""OAuth provider abstraction — framework-agnostic base classes and registry.

Third-party code can register custom providers without modifying stapel-auth:

    # In your app's AppConfig.ready():
    from stapel_core.oauth import register_provider
    from my_app.providers import MyProvider
    register_provider(MyProvider())
"""
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

    @abstractmethod
    def get_user_data(self, access_token: str) -> OAuthUserData | None:
        """Fetch and normalize user profile using the given access token."""
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
