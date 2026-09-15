"""Per-app settings namespaces — the DRF api_settings pattern, generalized.

Every Stapel package exposes one AppSettings instance instead of scattering
``getattr(settings, ...)`` calls:

    # stapel_billing/conf.py
    from stapel_core.conf import AppSettings

    billing_settings = AppSettings(
        "STAPEL_BILLING",
        defaults={
            "PAYMENT_PROVIDER": "stapel_billing.providers.stripe.StripeProvider",
            "CURRENCY": "usd",
        },
        import_strings=("PAYMENT_PROVIDER",),
    )

Resolution order per key: ``settings.<NAMESPACE>`` dict → flat Django
setting of the same name (legacy) → environment variable → default.
Values listed in *import_strings* are resolved with import_string — the
dotted-path escape hatch that makes behavior swappable without forking.
Caches are invalidated on Django's setting_changed (tests).

**A name in *import_strings* is never read from the environment.** Such a
name does not carry data, it names the CLASS the process imports and runs;
letting a same-named env var pick it means anything that can set an env var
in the pod (a leaked value, a sibling container's config, a stray export in
an entrypoint) chooses the implementation of a provider, backend or policy.
The project's own settings module is trusted and still wins — the
environment is not. A deployment that genuinely must select an
implementation from the environment says so once, by name, with
*env_overridable*.

Ignoring a variable is silent by nature, so the rule carries its own alarm:
``stapel_core.conf_checks`` walks :func:`registered_settings` and raises a
warning at ``manage.py check`` time for every env var that is set and not
read. Nobody has to remember to grep a manifest.
"""
from __future__ import annotations

import os
import weakref
from typing import Any, Iterable

_EMPTY = object()

# Declared shapes an environment string cannot carry. ``str``/``bytes`` are
# sequences too and are deliberately absent: a scalar IS what the environment
# is for.
#
# Why REFUSE rather than parse a declared format. ``_raw`` used to hand back
# ``os.environ.get(key)`` untouched, so ``DATA_OWNERS=auth,profiles`` was
# iterated character by character — a dozen owners named "a", "u", "t", "h",
# ",", every one of them a str, so the type checks passed and GDPR erasure was
# certified against nonsense. Two ways out, and only one of them is honest.
#
# A parser has to pick a format, and the format is wrong for the values these
# keys legally hold: ``DATA_OWNERS`` entries are a bare name OR a dict
# (``{"name": "cdn", "kind": "remote"}``), which no comma-split can express.
# Choosing comma-split would accept ``auth,profiles`` today and silently
# truncate the dict form tomorrow — the same class of defect, one layer up.
# JSON-in-env would express it, but then the same variable means two different
# things depending on the key's declared shape, and a typo produces a parse
# error at first read in production.
#
# A structured value has a correct home already: the ``STAPEL_<MOD>`` dict in
# the settings module, which the scaffold emits from the module's declared
# required settings. So the environment step is refused for these keys, loudly
# and by name — a refusal is a fact an operator can act on, where a lenient
# parse is a guess nobody reviews.
_STRUCTURED_TYPES = (list, tuple, set, frozenset, dict)

# Every AppSettings ever built, in construction order. The instances are
# per-module singletons living in ``<package>/conf.py`` modules that core
# cannot enumerate — siblings declare their own namespaces and core has no
# list of siblings. Registering at construction is the one place that sees
# all of them, and it lives on the class that already owns the semantics
# instead of a parallel list an author must remember to update. Weak, so a
# throwaway instance (a test's) does not pin memory; iteration order is the
# insertion order of the surviving entries.
_INSTANCES: list[weakref.ReferenceType] = []


def registered_settings() -> list["AppSettings"]:
    """Every live :class:`AppSettings` instance, in construction order.

    A namespace is only visible once its ``conf`` module has been imported —
    consumers that need the whole fleet (the ignored-env-var system check)
    must run after ``django.setup()``, when every installed app's modules
    are loaded.
    """
    live = [ref() for ref in _INSTANCES]
    if None in live:  # prune collected entries so the list cannot grow forever
        _INSTANCES[:] = [ref for ref, obj in zip(_INSTANCES, live) if obj is not None]
        live = [obj for obj in live if obj is not None]
    return live


class AppSettings:
    def __init__(
        self,
        namespace: str,
        defaults: dict[str, Any],
        import_strings: Iterable[str] = (),
        no_env: Iterable[str] = (),
        env_overridable: Iterable[str] = (),
        resolvers: dict[str, Any] | None = None,
        env_enum: dict[str, Any] | None = None,
    ) -> None:
        self.namespace = namespace
        self.defaults = dict(defaults)
        self.import_strings = frozenset(import_strings)
        # Keys whose raw string is turned into an object by a CALLABLE of the
        # declaring package's choosing instead of by bare ``import_string``.
        #
        # Policy-wise a resolver key is a MEMBER OF THE import_strings FAMILY:
        # implicitly env-closed, reopened only by env_overridable, reported by
        # W001 with the class wording. Only the string→object step is
        # delegated. That is deliberate — a free-floating "resolve this key"
        # hook divorced from the family would be a third kind of key with
        # unspecified environment semantics: new surface, no gate.
        #
        # The need is real and three times proven: a value that is legally
        # "registry short name OR dotted path" cannot go through the base
        # class's eager import_string, so packages either subclassed around
        # __getattr__ (stapel-notifications) or kept the key out of
        # import_strings with a comment (stapel-auth's OAUTH_PROVIDER_CLASSES)
        # — losing the declaration, and with it the W001 alarm. Declaring
        # ``no_env`` instead would have closed the door with no way back out:
        # ``no_env ∩ env_overridable`` is a construction error by design.
        #
        # A resolver may be given as a dotted path, resolved lazily at first
        # use, so a package's conf.py never imports a channel module at
        # declaration time.
        self.resolvers: dict[str, Any] = dict(resolvers or {})
        # Keys that must never fall back to an environment variable: their
        # names are generic enough (PROVIDER, TRUSTED_PROXY_HEADER, …) that a
        # stray same-named env var could silently change a trust/security
        # decision. They still resolve via the namespace dict, a flat Django
        # setting, or the default — an explicit env var is simply ignored.
        self.no_env = frozenset(no_env)
        # The deliberate, greppable opt-OUT of the implicit no_env that every
        # import_strings key gets. Naming a key here restores the environment
        # step for it — for the deployment that really does pick an
        # implementation per environment. It is opt-out on purpose: forgetting
        # a flag must leave the process safe, never open.
        self.env_overridable = frozenset(env_overridable)
        # The NARROW way back out of the implicit closure: a key whose value
        # is legally "a short name from a closed registry OR a dotted path"
        # may be set from the environment, but only to a SHORT NAME.
        #
        # Why the third family exists at all. ``env_overridable`` is
        # all-or-nothing, and for these keys neither end is right: closed, and
        # the deployment cannot pick its mail backend per environment without
        # every host re-implementing ``os.getenv`` in its settings module (one
        # fleet did exactly that, and the documented variable then worked ONLY
        # there, with W001 truthfully reporting it ignored — a documented
        # surface that lies is worse than either honest answer). Open, and an
        # environment variable can name an ARBITRARY DOTTED PATH — which is
        # precisely the threat ``_names_a_class`` exists to block: whatever can
        # set a variable in the pod chooses the class on the privileged path.
        #
        # The enumeration splits the difference exactly along the line of the
        # threat. Picking among implementations the LIBRARY ships is a
        # deployment choice and belongs in the environment; naming new code to
        # import is a trust decision and stays in the settings module, which
        # only the project can write. An out-of-vocabulary value is refused
        # loudly rather than ignored — see ``_refuse_env_enum``.
        #
        # The vocabulary may be an iterable, a zero-arg callable, or a dotted
        # path to one. Callable and dotted forms are resolved LAZILY, at the
        # first read: a registry is usually a dict in a channel/provider
        # module, and importing it from a package's ``conf.py`` at declaration
        # time would drag that module into every import of the package.
        self.env_enum: dict[str, Any] = dict(env_enum or {})
        both_ways_out = set(self.env_enum) & self.env_overridable
        if both_ways_out:
            raise ValueError(
                f"{namespace}: {sorted(both_ways_out)} declared both env_enum "
                "and env_overridable — env_overridable already allows any "
                "value including a dotted path, so the enumeration would be "
                "decorative. Say which one you mean"
            )
        enum_closed = set(self.env_enum) & self.no_env
        if enum_closed:
            raise ValueError(
                f"{namespace}: {sorted(enum_closed)} declared both env_enum "
                "and no_env — one opens the environment step, the other "
                "closes it. Say which one you mean"
            )
        unknown_enum = set(self.env_enum) - set(self.defaults)
        if unknown_enum:
            # An enumeration on a key this namespace does not have is a typo
            # that would silently never apply — the exact silence this family
            # was added to end.
            raise ValueError(
                f"{namespace}: env_enum names {sorted(unknown_enum)}, which "
                f"{'is' if len(unknown_enum) == 1 else 'are'} not in defaults"
            )
        contradictory = self.no_env & self.env_overridable
        if contradictory:
            # Silently picking a winner would hide an authoring mistake in the
            # one declaration that decides what code the process loads.
            raise ValueError(
                f"{namespace}: {sorted(contradictory)} declared both no_env and "
                "env_overridable — say which one you mean"
            )
        double_resolved = self.import_strings & set(self.resolvers)
        if double_resolved:
            # Two answers to "what turns this string into an object" is an
            # authoring mistake in the one declaration that decides what code
            # the process loads. Picking a winner silently would hide it.
            raise ValueError(
                f"{namespace}: {sorted(double_resolved)} declared both "
                "import_strings and resolvers — a resolver key already "
                "belongs to the import_strings family; list it in one place"
            )
        self._cache: dict[str, Any] = {}
        self._connect_reload()
        _INSTANCES.append(weakref.ref(self))

    def env_var_names(self, key: str) -> tuple[str, ...]:
        """Environment variable names ``_raw`` consults for *key*, in order.

        The one place the naming convention lives. A check that reports env
        vars this namespace ignores asks the same method, so the guard cannot
        drift from the thing it guards. Subclasses that read other names
        (through the gate) extend this.
        """
        return (key,)

    def _names_a_class(self, key: str) -> bool:
        """Does *key* name the implementation the process loads?

        True for import_strings and for resolver keys — the resolver family
        is the import_strings family with a custom string→object step, so it
        answers this question the same way and gets the same wording.
        """
        return key in self.import_strings or key in self.resolvers

    def _env_allowed(self, key: str) -> bool:
        """May *key* be read from ``os.environ``?

        A key that names an implementation, not a value, is implicitly
        no_env; ``env_overridable`` is the explicit way back out, and
        ``env_enum`` is the narrow one — the environment may choose among the
        library's own short names, and the VALUE is then checked in ``_raw``.
        """
        if key in self.no_env:
            return False
        if self._names_a_class(key):
            return key in self.env_overridable or key in self.env_enum
        return True

    def env_vocabulary(self, key: str) -> tuple[str, ...]:
        """The short names ``key`` may be set to from the environment.

        Resolved at first read and memoised in place, so a package's
        ``conf.py`` never imports its provider modules at declaration time.
        A vocabulary that cannot be resolved is an authoring error in the
        declaration, not an operator error, so it propagates rather than
        degrading to "nothing is allowed" — which would read to an operator
        exactly like a rejected value.
        """
        vocabulary = self.env_enum[key]
        if isinstance(vocabulary, str):
            from django.utils.module_loading import import_string

            vocabulary = import_string(vocabulary)
            self.env_enum[key] = vocabulary
        if callable(vocabulary):
            vocabulary = vocabulary()
        return tuple(str(name) for name in vocabulary)

    def _refuse_env_enum(self, key: str, name: str, raw: str):
        """An environment value outside the key's vocabulary. Always loud.

        Refused rather than ignored, and that is the whole point of the
        family. Ignoring would leave the process on a safe default — which is
        what the blanket closure already did, and what produced a documented
        variable that quietly did nothing. Once an operator has been TOLD the
        variable works, a value it does not accept is a live misconfiguration
        and deserves an answer, not a shrug.

        The two failure modes get different sentences because they need
        different fixes: a dotted path is a trust decision in the wrong file,
        a bare name is usually a typo.
        """
        from django.core.exceptions import ImproperlyConfigured

        known = ", ".join(sorted(self.env_vocabulary(key))) or "(none)"
        if "." in raw:
            detail = (
                f"{raw!r} looks like a dotted import path. A path may only be "
                f"set in the {self.namespace} dict in your settings module: "
                "naming new code to import is a trust decision, and anything "
                "able to set a variable in this process's environment would "
                "otherwise choose the class on the privileged path."
            )
        else:
            detail = (
                f"{raw!r} is not one of the names this library ships. "
                f"Known names: {known}."
            )
        raise ImproperlyConfigured(
            f"Environment variable {name} cannot set "
            f'{self.namespace}["{key}"]: {detail} '
            f"Unset {name} to fall back to the settings value or the default."
        )

    def env_closed_keys(self) -> list[str]:
        """Every key whose environment step this instance closes, sorted.

        The union of both closing families — ``no_env`` and the implicit
        closure over import_strings/resolvers minus ``env_overridable`` —
        computed by asking ``_env_allowed`` about every key this namespace
        knows, so the walk can never ask a narrower question than the gate
        answers.
        """
        known = set(self.defaults) | self.no_env | self.import_strings | set(self.resolvers)
        return sorted(key for key in known if not self._env_allowed(key))

    def ignored_env_vars(self) -> list[tuple[str, str, str]]:
        """``(key, env var name, family)`` triples that are SET and unread.

        Scope is EVERY key the instance closes, not just *import_strings*.
        The narrow scope had no hidden justification and reproduced the very
        silence W001 was built to end: ``no_env`` was invented for the same
        threat model (generic names that could "silently change a
        trust/security decision"), so a set-but-ignored env var on a no_env
        key is precisely "the operator believes X is configured and it is
        not".

        The noise worry — generic names like ``BACKEND`` or ``PROVIDER``
        colliding with unrelated variables — is answered by W-level severity,
        by the per-namespace dedup the check already does, and by the fact
        that the collision IS the information: that variable is being
        ignored.

        *family* is ``"class"`` when the key names the implementation the
        process loads and ``"policy"`` when it is a no_env value key, so the
        warning can say which rule closed the key and never over-claim.
        Names are still spelled exclusively by ``env_var_names`` — a
        namespace whose alias route never entered that method (media's
        ``STAPEL_MEDIA_BACKEND``) is correctly not reported, because that
        alias IS honored and the check must not claim it was dropped.
        """
        return [
            (key, name, "class" if self._names_a_class(key) else "policy")
            for key in self.env_closed_keys()
            for name in self.env_var_names(key)
            if name in os.environ
        ]

    def structured_keys(self) -> list[str]:
        """Keys whose DECLARED SHAPE is a container, sorted.

        The shape is the type of the declared default: a key that defaults to
        ``[]`` is a list of things, and no bare environment string is one.
        """
        return sorted(
            key for key, default in self.defaults.items()
            if isinstance(default, _STRUCTURED_TYPES)
        )

    def structured_env_vars(self) -> list[tuple[str, str]]:
        """``(key, env var name)`` pairs that are SET on a container-shaped key.

        The boot-time half of the refusal in :meth:`_raw` — the process-wide
        walk a system check reports, so the misconfiguration is found at
        deploy rather than the first time some code path happens to read the
        key.
        """
        return [
            (key, name)
            for key in self.structured_keys()
            if self._env_allowed(key)
            for name in self.env_var_names(key)
            if name in os.environ
        ]

    def rejected_env_enum_vars(self) -> list[tuple[str, str, str, tuple[str, ...]]]:
        """``(key, env var, value, vocabulary)`` for every enum key set wrong.

        The boot-time half of :meth:`_refuse_env_enum`, for the same reason
        ``structured_env_vars`` exists: "the moment the key is read" can be
        the first time an OTP is sent, which on a notifications service is
        after the deploy looked green. An operator who set the variable
        deserves the answer at ``manage.py check``.

        A vocabulary that cannot be resolved is skipped here rather than
        raised: a system check that dies takes every OTHER finding with it,
        and an unimportable registry is an authoring bug that ``_raw`` will
        raise about with the full traceback at the point of use.
        """
        found = []
        for key in self.env_enum:
            if not self._env_allowed(key):
                continue
            try:
                vocabulary = self.env_vocabulary(key)
            except Exception:
                continue
            for name in self.env_var_names(key):
                raw = os.environ.get(name)
                if raw is not None and raw not in vocabulary:
                    found.append((key, name, raw, vocabulary))
        return found

    def _refuse_structured_env(self, key: str, name: str, raw: str):
        from django.core.exceptions import ImproperlyConfigured

        shape = type(self.defaults[key]).__name__
        raise ImproperlyConfigured(
            f'{self.namespace}["{key}"] is declared as a {shape}, and the '
            f"environment variable {name}={raw!r} is a bare string. Reading it "
            f"as the value would iterate it CHARACTER BY CHARACTER: "
            f'"auth,profiles" becomes thirteen entries named "a", "u", "t", '
            '"h", ",", … — each of them a str, so every downstream type check '
            "passes and the result is certified nonsense. Put the value in "
            f"the {self.namespace} dict in your settings module (the "
            "scaffold emits that block from the module's declared required "
            f"settings), or unset {name}."
        )

    def _connect_reload(self) -> None:
        try:
            from django.test.signals import setting_changed

            def _reload(*, setting, **kwargs):
                if setting == self.namespace or setting in self.defaults:
                    self.reload()

            setting_changed.connect(_reload, weak=False)
        except Exception:  # Django not ready — tests will call reload()
            pass

    def reload(self) -> None:
        self._cache.clear()

    def _raw(self, key: str) -> Any:
        from django.conf import settings

        overrides = getattr(settings, self.namespace, None) or {}
        if key in overrides:
            return overrides[key]
        flat = getattr(settings, key, _EMPTY)
        if flat is not _EMPTY:
            return flat
        if self._env_allowed(key):
            for name in self.env_var_names(key):
                env = os.environ.get(name)
                if env is not None:
                    if isinstance(self.defaults.get(key), _STRUCTURED_TYPES):
                        self._refuse_structured_env(key, name, env)
                    if key in self.env_enum and env not in self.env_vocabulary(key):
                        self._refuse_env_enum(key, name, env)
                    return env
        if key in self.defaults:
            return self.defaults[key]
        raise AttributeError(f"{self.namespace} has no setting {key!r}")

    def _resolver_for(self, key: str):
        """The callable that turns *key*'s raw string into an object.

        A dotted-path resolver is imported HERE, at first use, not at
        declaration time: a package's ``conf.py`` must not drag its channel
        modules into every import of the package. The imported callable
        replaces the string in place, so the import happens once.
        """
        resolver = self.resolvers[key]
        if isinstance(resolver, str):
            from django.utils.module_loading import import_string

            resolver = import_string(resolver)
            self.resolvers[key] = resolver
        return resolver

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if key in self._cache:
            return self._cache[key]
        value = self._raw(key)
        if isinstance(value, str) and value:
            if key in self.resolvers:
                # Errors pass through untouched on purpose: a registry
                # resolver raises ImproperlyConfigured with a message that
                # names the short names it does know, and wrapping that in
                # ImportError would throw away the only useful half.
                value = self._resolver_for(key)(value)
            elif key in self.import_strings:
                from django.utils.module_loading import import_string

                value = import_string(value)
        self._cache[key] = value
        return value


__all__ = ["AppSettings", "registered_settings"]
