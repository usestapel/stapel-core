"""Stapel Core — shared Django utilities for the Stapel framework.

The building blocks every Stapel package sits on:

- ``comm`` — Action/Function inter-module communication (``emit``,
  ``on_action``, ``call``, ``function``) plus long-running tasks
  (``start``, ``status``, ``task_handler``). Transports are deployment
  configuration, not code.
- ``bus`` — transport-agnostic message bus (``publish``, ``get_bus``,
  ``Event``) with Kafka/NATS/in-memory backends.
- ``conf.AppSettings`` — per-app settings namespaces (the DRF
  ``api_settings`` pattern, generalized).
- ``signals`` — in-process Django signals for business milestones.
- ``django.api`` — API conventions: ``StapelResponse``,
  ``StapelErrorResponse`` and ``StapelDataclassSerializer``.
- ``gdpr`` — GDPR provider protocol and in-process registry.
- ``django.users.AbstractStapelUser`` — base user model.
- ``core`` — framework-agnostic JWT primitives (``JWTHandler``,
  ``TokenManager``, ``TokenBlacklist``, ``JWTConfig``).

All attributes are exported lazily (PEP 562), so importing
``stapel_core`` stays cheap and never touches Django until a
Django-dependent attribute is actually used.
"""

from importlib import import_module

__version__ = "0.1.0"

# Attribute name -> (relative module, attribute in that module).
# An attribute of None means the module itself is the export.
_LAZY_EXPORTS = {
    # comm — Actions, Functions, long-running tasks
    "emit": (".comm", "emit"),
    "on_action": (".comm", "on_action"),
    "call": (".comm", "call"),
    "function": (".comm", "function"),
    "start": (".comm", "start"),
    "status": (".comm", "status"),
    "task_handler": (".comm", "task_handler"),
    # flows — self-documenting business scenarios
    "Flow": (".flows", "Flow"),
    "flow_step": (".flows", "flow_step"),
    "flow_registry": (".flows", "flow_registry"),
    # verification — step-up (OTP/TOTP/passkey) on any endpoint
    "requires_verification": (".verification", "requires_verification"),
    "register_factor": (".verification", "register_factor"),
    "VerificationFactor": (".verification", "VerificationFactor"),
    "get_user_policy": (".verification", "get_user_policy"),
    "invalidate_policy_cache": (".verification", "invalidate_policy_cache"),
    # bus — transport-agnostic message bus
    "publish": (".bus", "publish"),
    "get_bus": (".bus", "get_bus"),
    "Event": (".bus", "Event"),
    # conf — per-app settings namespaces
    "AppSettings": (".conf", "AppSettings"),
    # signals — in-process business milestones (module export)
    "signals": (".signals", None),
    # API conventions — responses, errors, serializers
    "StapelResponse": (".django.api.errors", "StapelResponse"),
    "StapelErrorResponse": (".django.api.errors", "StapelErrorResponse"),
    "StapelDataclassSerializer": (".django.api.serializers", "StapelDataclassSerializer"),
    # GDPR — provider protocol + in-process registry
    "GDPRProvider": (".gdpr", "GDPRProvider"),
    "gdpr_registry": (".gdpr", "gdpr_registry"),
    # Users — base user model
    "AbstractStapelUser": (".django.users.models", "AbstractStapelUser"),
    # Framework-agnostic JWT primitives (0.1.x root exports, kept stable)
    "JWTHandler": (".core.jwt_handler", "JWTHandler"),
    "TokenManager": (".core.token_manager", "TokenManager"),
    "TokenBlacklist": (".core.token_blacklist", "TokenBlacklist"),
    "JWTConfig": (".core.config", "JWTConfig"),
}

__all__ = sorted([*_LAZY_EXPORTS, "__version__"])


def __getattr__(name):
    try:
        module_path, attr = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module = import_module(module_path, __name__)
    value = module if attr is None else getattr(module, attr)
    globals()[name] = value  # cache so __getattr__ runs once per name
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
