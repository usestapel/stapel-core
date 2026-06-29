"""
Common Django settings for Iron services.

Usage:
    from stapel_core.django.settings import *

This provides all common settings that can be used directly or overridden.
"""
import os
import logging
from pathlib import Path
from typing import List, Optional

__all__ = [
    # Database
    "get_default_database",
    # Logging
    "LOGGING",
    # Host configuration
    "IRON_HOST",
    # Django core
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    # Common apps and middleware
    "COMMON_INSTALLED_APPS",
    "COMMON_MIDDLEWARE",
    "get_common_templates",
    "DEFAULT_CACHE",
    # Auth
    "AUTH_USER_MODEL",
    "AUTHENTICATION_BACKENDS",
    "AUTH_PASSWORD_VALIDATORS",
    "SECRET_KEY",
    # Proxy & SSL
    "SECURE_PROXY_SSL_HEADER",
    # Session & CSRF
    "SESSION_ENGINE",
    "SESSION_CACHE_ALIAS",
    "SESSION_COOKIE_NAME",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_SECURE",
    "CSRF_USE_SESSIONS",
    "LOGIN_URL",
    "LOGOUT_REDIRECT_URL",
    # REST Framework
    "REST_FRAMEWORK",
    "SPECTACULAR_SETTINGS",
    # JWT
    "JWT_ACCESS_TOKEN_LIFETIME",
    "JWT_REFRESH_TOKEN_LIFETIME",
    "JWT_COOKIE_NAME",
    "JWT_REFRESH_COOKIE_NAME",
    "JWT_COOKIE_DOMAIN",
    "JWT_COOKIE_SECURE",
    "JWT_COOKIE_HTTPONLY",
    "JWT_COOKIE_SAMESITE",
    "JWT_AUTO_REFRESH_ENABLED",
    "JWT_REFRESH_THRESHOLD",
    "JWT_REFRESH_ALLOWED",
    "JWT_ALGORITHM",
    "JWT_SECRET_KEY",
    "JWT_PRIVATE_KEY",
    "JWT_PUBLIC_KEY",
    "JWT_ISSUER",
    "JWT_AUDIENCE",
    # Service API
    "SERVICE_API_KEY",
    "SERVICE_API_KEYS",
    # Kafka
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SECURITY_PROTOCOL",
    # CORS
    "CORS_ALLOW_ALL_ORIGINS",
    "CORS_ALLOWED_ORIGINS",
    # i18n
    "LANGUAGE_CODE",
    "TIME_ZONE",
    "USE_I18N",
    "USE_TZ",
    # Static files
    "COMMON_STATIC_DIR",
    "get_staticfiles_dirs",
    # Sentry
    "setup_sentry",
]

_log_level = os.getenv('LOG_LEVEL', 'INFO')

_TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
_TELEGRAM_ALERT_CHAT_ID = os.getenv('TELEGRAM_ALERT_CHAT_ID', '')
_TELEGRAM_ALERT_THREAD_ID = os.getenv('TELEGRAM_ALERT_THREAD_ID', '')
_SERVICE_NAME = os.getenv('SERVICE_NAME', '')

_telegram_configured = bool(_TELEGRAM_BOT_TOKEN and _TELEGRAM_ALERT_CHAT_ID)

_handlers = {
    'console': {
        'class': 'logging.StreamHandler',
        'formatter': 'verbose',
    },
}
_root_handlers = ['console']

if _telegram_configured:
    _handlers['telegram'] = {
        'class': 'stapel_core.django.monitoring.telegram.TelegramHandler',
        'level': 'ERROR',
        'service': _SERVICE_NAME,
    }
    _root_handlers.append('telegram')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': _handlers,
    'root': {
        'handlers': _root_handlers,
        'level': _log_level,
    },
    'loggers': {
        'django': {
            'level': 'WARNING',
        },
        'django.request': {
            'level': 'WARNING',
        },
    },
}

# drf-spectacular OpenAPI settings for all services
# Services should extend this with their own TITLE and DESCRIPTION
SPECTACULAR_SETTINGS = {
    'TITLE': 'Iron API',
    'DESCRIPTION': 'Iron Platform API',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,

    # OpenAPI 3.0 security schemes
    'SECURITY': [{'bearerAuth': []}],
    'APPEND_COMPONENTS': {
        'securitySchemes': {
            'bearerAuth': {
                'type': 'http',
                'scheme': 'bearer',
                'bearerFormat': 'JWT',
                'description': 'JWT authorization. Example: "Authorization: Bearer {token}"'
            }
        }
    },

    # Schema generation settings
    'COMPONENT_SPLIT_REQUEST': True,
    'SCHEMA_PATH_PREFIX': r'/api/',

    # Tag extraction from URL path (first segment after /api/)
    # e.g., /api/auth/login/ -> "auth", /api/categories/ -> "categories"
    'PREPROCESSING_HOOKS': ['stapel_core.django.openapi.schemas.preprocess_exclude_schema_endpoints'],
    'POSTPROCESSING_HOOKS': [
        'stapel_core.django.openapi.schemas.postprocess_schema_tags',
        'stapel_core.django.openapi.schemas.postprocess_fix_polymorphic_discriminators',
    ],

    # Swagger UI settings
    'SWAGGER_UI_SETTINGS': {
        'deepLinking': True,
        'persistAuthorization': True,
        'displayOperationId': False,
        'tagsSorter': 'alpha',
        'operationsSorter': 'alpha',
    },

    # Disable session auth in Swagger UI
    'SERVE_PERMISSIONS': ['rest_framework.permissions.AllowAny'],
}

# JWT Configuration for common library - read from environment
JWT_ACCESS_TOKEN_LIFETIME = int(os.getenv('JWT_ACCESS_TOKEN_LIFETIME', '3600'))  # 1 hour default
JWT_REFRESH_TOKEN_LIFETIME = int(os.getenv('JWT_REFRESH_TOKEN_LIFETIME', '604800'))  # 7 days default
JWT_COOKIE_NAME = os.getenv('JWT_COOKIE_NAME', 'iron_jwt')
JWT_REFRESH_COOKIE_NAME = os.getenv('JWT_REFRESH_COOKIE_NAME', 'iron_refresh_jwt')
JWT_COOKIE_DOMAIN = os.getenv('JWT_COOKIE_DOMAIN', None)  # None = host-only, set to ".domain.com" for subdomains
JWT_COOKIE_SECURE = os.getenv('JWT_COOKIE_SECURE', 'False').lower() == 'true'  # True in production with HTTPS
JWT_COOKIE_HTTPONLY = os.getenv('JWT_COOKIE_HTTPONLY', 'True').lower() == 'true'
JWT_COOKIE_SAMESITE = os.getenv('JWT_COOKIE_SAMESITE', 'Lax')
JWT_AUTO_REFRESH_ENABLED = os.getenv('JWT_AUTO_REFRESH_ENABLED', 'False').lower() == 'true'
JWT_REFRESH_THRESHOLD = int(os.getenv('JWT_REFRESH_THRESHOLD', '300'))  # 5 minutes default
# Only auth service should be allowed to refresh tokens (set True in auth service settings)
JWT_REFRESH_ALLOWED = os.getenv('JWT_REFRESH_ALLOWED', 'False').lower() == 'true'

# CORS Configuration for all services
# For production, set CORS_ALLOWED_ORIGINS environment variable with comma-separated origins
# For local development, set CORS_ALLOW_ALL_ORIGINS=true in .env
CORS_ALLOW_ALL_ORIGINS = os.getenv('CORS_ALLOW_ALL_ORIGINS', 'False').lower() == 'true'
cors_origins = os.getenv('CORS_ALLOWED_ORIGINS', '')
CORS_ALLOWED_ORIGINS = [origin.strip() for origin in cors_origins.split(',') if origin.strip()]
# Allow cookies to be sent with cross-origin requests
CORS_ALLOW_CREDENTIALS = True

# Common app lists and middleware scaffolding (services can extend/override)
COMMON_INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "common.django.apps.CommonDjangoConfig",
    "common.django.users",
]

COMMON_MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "common.django.middleware.CsrfExemptAPIMiddleware",  # Must be before CsrfViewMiddleware
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "common.django.middleware.JWTAuthMiddleware",
    "common.django.admin_redirect_middleware.AdminLoginRedirectMiddleware",
    "common.django.middleware.ServiceAPIKeyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

def get_common_templates(base_dir: Path) -> List[dict]:
    """Return template config with common dirs and context."""
    return [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "DIRS": [base_dir / "templates", "/app/stapel_core/django/templates"],
            "APP_DIRS": True,
            "OPTIONS": {
                "context_processors": [
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                    "common.django.admin_context.iron_services",
                ],
            },
        },
    ]


# Path to common static files (for STATICFILES_DIRS)
COMMON_STATIC_DIR = "/app/stapel_core/static"


def get_staticfiles_dirs(base_dir: Path) -> List[str]:
    """Return STATICFILES_DIRS including common static files."""
    dirs = []
    # Add service-specific static dir if exists
    service_static = base_dir / "static"
    if service_static.exists():
        dirs.append(str(service_static))
    # Add common static dir
    dirs.append(COMMON_STATIC_DIR)
    return dirs

def get_default_cache(redis_url: Optional[str] = None) -> dict:
    """Shared redis cache config."""
    redis_url = redis_url or os.getenv("REDIS_URL", "redis://redis:6379/0")
    return {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": redis_url,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }

DEFAULT_CACHE = get_default_cache()


def get_default_database(
    db_name: str,
    engine: str = 'django.db.backends.postgresql',
) -> dict:
    """Shared database config for use with pgbouncer (transaction mode).

    CONN_MAX_AGE=0: Django opens/closes per request (cheap via pgbouncer).
    DISABLE_SERVER_SIDE_CURSORS: required for pgbouncer transaction mode.
    CONN_HEALTH_CHECK: validates connection before reuse.

    Args:
        db_name: Default database name (overridden by POSTGRES_DB env var).
        engine: Database engine (use 'django.contrib.gis.db.backends.postgis' for geo).
    """
    return {
        'ENGINE': engine,
        'NAME': os.getenv('POSTGRES_DB', db_name),
        'USER': os.getenv('POSTGRES_USER', 'iron'),
        'PASSWORD': os.getenv('POSTGRES_PASSWORD', 'iron'),
        'HOST': os.getenv('POSTGRES_HOST', 'db'),
        'PORT': os.getenv('POSTGRES_PORT', '5432'),
        'CONN_MAX_AGE': int(os.getenv('CONN_MAX_AGE', '0')),
        'CONN_HEALTH_CHECK': True,
        'DISABLE_SERVER_SIDE_CURSORS': True,
    }

# Trust X-Forwarded-Proto from nginx (all services run behind reverse proxy)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Session defaults (services can override domain/secure in prod)
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_NAME = "iron_sessionid"
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = False  # set True in prod

# CSRF cookie-based (default) - works better with microservices
# JS must call syncCsrfToken() after AJAX requests to update form tokens
CSRF_USE_SESSIONS = False
# Note: Each service should override CSRF_COOKIE_NAME in their base.py settings

# Default login URLs (can override per service)
LOGIN_URL = "/auth/admin/login/"
LOGOUT_REDIRECT_URL = "/auth/admin/login/"

# REST Framework sensible defaults (can be overridden)

# REST Framework configuration for all services
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'stapel_core.django.jwt.authentication.JWTCookieAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'stapel_core.django.api.permissions.IsServiceRequest',
        'stapel_core.django.api.permissions.IsSuperUser',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ],
    # Custom AutoSchema that displays permission classes in Swagger
    'DEFAULT_SCHEMA_CLASS': 'stapel_core.django.openapi.schemas.PermissionAwareAutoSchema',
    'EXCEPTION_HANDLER': 'stapel_core.django.api.errors.iron_exception_handler',
}

# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

# Custom User Model
AUTH_USER_MODEL = 'users.User'

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-auth-service-change-this-in-production')

# =============================================================================
# HOST CONFIGURATION
# =============================================================================
# IRON_HOST is the primary host variable (e.g., "stg.iron.com", "iron.com")
# Other host-dependent settings are derived from it
IRON_HOST = os.getenv('IRON_HOST', 'localhost')

# Allowed hosts - can be overridden via ALLOWED_HOSTS env, otherwise derived from IRON_HOST
_allowed_hosts = os.getenv('ALLOWED_HOSTS', '')
if _allowed_hosts:
    ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts.split(',') if h.strip()]
else:
    # Auto-generate from IRON_HOST
    ALLOWED_HOSTS = [IRON_HOST, 'localhost', '127.0.0.1']

# CSRF trusted origins - required for Django 4+ with HTTPS
# Can be set via CSRF_TRUSTED_ORIGINS env var (comma-separated)
# Or auto-generated from IRON_HOST
_csrf_origins = os.getenv('CSRF_TRUSTED_ORIGINS', '')
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(',') if o.strip()]
else:
    CSRF_TRUSTED_ORIGINS = [f'https://{IRON_HOST}'] if IRON_HOST != 'localhost' else []

# JWT Configuration
# Supports both symmetric (HS256) and asymmetric (RS256) algorithms
# For RS256, set JWT_PRIVATE_KEY and/or JWT_PUBLIC_KEY (base64 encoded PEM)
# Auth service needs private key for signing; other services only need public key for verification
import base64
_jwt_private_key_b64 = os.getenv('JWT_PRIVATE_KEY', '')
_jwt_public_key_b64 = os.getenv('JWT_PUBLIC_KEY', '')

# Auto-detect algorithm: RS256 if any RSA key is present, otherwise HS256
_has_rsa_keys = bool(_jwt_private_key_b64 or _jwt_public_key_b64)
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'RS256' if _has_rsa_keys else 'HS256')
JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', SECRET_KEY)

# RSA keys for RS256 (base64 encoded PEM format)
JWT_PRIVATE_KEY = base64.b64decode(_jwt_private_key_b64).decode('utf-8') if _jwt_private_key_b64 else ''
JWT_PUBLIC_KEY = base64.b64decode(_jwt_public_key_b64).decode('utf-8') if _jwt_public_key_b64 else ''

# JWT issuer and audience - derived from IRON_HOST if not explicitly set
JWT_ISSUER = os.getenv('JWT_ISSUER', f'https://{IRON_HOST}')
JWT_AUDIENCE = os.getenv('JWT_AUDIENCE', 'iron')

# Message bus backend
# Options:
#   stapel_core.bus.backends.kafka.KafkaBus   — production (default)
#   stapel_core.bus.backends.memory.MemoryBus — tests / local dev without a broker
STAPEL_BUS_BACKEND = os.getenv(
    'STAPEL_BUS_BACKEND',
    'stapel_core.bus.backends.kafka.KafkaBus',
)

# Kafka Configuration (used when STAPEL_BUS_BACKEND = KafkaBus)
KAFKA_BOOTSTRAP_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'kafka:9092')
KAFKA_SECURITY_PROTOCOL = os.getenv('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT')
KAFKA_SASL_MECHANISM = os.getenv('KAFKA_SASL_MECHANISM', '')
KAFKA_SASL_USERNAME = os.getenv('KAFKA_CLIENT_USER', '')
KAFKA_SASL_PASSWORD = os.getenv('KAFKA_CLIENT_PASSWORD', '')

# Shared service-to-service API key (preferred)
SERVICE_API_KEY = os.getenv('SERVICE_API_KEY')
# Optional per-service map (backward compatible)
SERVICE_API_KEYS = {
}

# Authentication backends
# Note: JWTAuthBackend is not needed because JWTAuthMiddleware handles
# user creation automatically. We only need the default ModelBackend.
AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',  # Default Django auth
]


# =============================================================================
# SENTRY
# =============================================================================

def setup_sentry(service_name: str) -> None:
    """
    Initialize Sentry SDK for a Django service.

    Call from the service's base settings after importing common settings:
        setup_sentry("catalog")

    Required env vars:
        SENTRY_DSN - DSN from Sentry project settings (skip init if empty)
    Optional env vars:
        SENTRY_ENVIRONMENT          - dev / stg / prod (default: "dev")
        SENTRY_TRACES_SAMPLE_RATE   - 0.0-1.0 (default: 1.0 for dev, 0.1 for others)
        SENTRY_PROFILES_SAMPLE_RATE - 0.0-1.0 (default: 1.0 for dev, 0.1 for others)
        SENTRY_SEND_DEFAULT_PII     - true/false (default: false)
    """
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        return

    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return

    environment = os.getenv("SENTRY_ENVIRONMENT", "dev")
    default_sample_rate = "1.0" if environment == "dev" else "0.1"

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", default_sample_rate)),
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", default_sample_rate)),
        send_default_pii=os.getenv("SENTRY_SEND_DEFAULT_PII", "false").lower() == "true",
        server_name=service_name,
    )
