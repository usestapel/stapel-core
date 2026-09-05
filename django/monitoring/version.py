"""
``/api/version/`` — what this process is actually running, asked from outside.

─── Why this is not a field on ``/api/health/`` ────────────────────────────

It nearly is. ``health_check`` already reports ``version``, and it reports
``settings.APP_VERSION_NUMBER`` — a string read from a per-service
``version.txt`` that has said ``0.1.0`` in every service of a fleet since the
day each was scaffolded. It answers "which release of this service's own thin
config wrapper", which nobody has ever wanted to know, and it says nothing
about the libraries the wrapper is made of, which is the only thing anyone
asks. Two questions were sharing a field, and the useless one had it.

The question people actually ask is a version claim about a LIBRARY: "is the
fix in?", "is this stand on stapel-search 0.7.0 or 0.3.1?". A fleet already
knows how to answer that from the inside — a client fleet's deploy gate
reads ``pip list`` inside every container to catch a service running two
builds of itself, which it does because that exact skew once made
``/suggest`` answer ``degraded`` with every healthcheck green.

What nobody could do was ask from OUTSIDE. A walker measuring a stand, or an
owner with curl, had no way to distinguish a fix that does not work from a
fix that was never deployed, and a coordinator's "I deployed X" was
unverifiable by anyone but the coordinator.

─── Everything here is read, never declared ────────────────────────────────

``importlib.metadata`` reports what is INSTALLED in the running interpreter —
the same source ``pip list`` reads, asked in-process. Not ``requirements.txt``
(which says what an image was built FROM, and the whole failure mode is a
container not running the image that file describes) and not a constant in a
settings module (which is correct on the day it is typed and plausible
forever after).

The build identity — commit, image tag, build time — is the one thing a
Python process genuinely cannot derive, so it is read from the environment and
reported as ``null`` when nobody set it. Absent is said as absent: an
unstamped image gets ``"commit": null``, never a guess, never a stale value
left over from the last time someone edited a config.

─── What this exposes ──────────────────────────────────────────────────────

The version numbers of open-source libraries, on a surface that already
publishes its full OpenAPI schema and its dependency behaviour. That is a
deliberate trade and a small one, but it is a trade: set
``STAPEL_VERSION_ENDPOINT["PUBLIC"] = False`` and the view answers only staff
(``request.user.is_staff``), which keeps the outside observer a *credentialed*
outside observer rather than removing the answer.

Usage — nothing to wire, ``get_health_urls()`` already mounts it::

    urlpatterns = [*get_health_urls('myservice/')]
    # GET /myservice/api/version/

Env the image should stamp (the deployment's job, see the fleet's Dockerfile)::

    STAPEL_GIT_SHA      the commit the image was built from, optionally
                        suffixed ``-dirty`` (a fleet Makefile's own stamp for
                        "built from an uncommitted tree") — parsed off into
                        ``dirty`` rather than left in ``commit``
    STAPEL_IMAGE_NAME   e.g. svc-classified-core
    STAPEL_IMAGE_TAG    e.g. sha-2bdb7898
    STAPEL_BUILD_TIME   ISO-8601, UTC
    STAPEL_BUILD_DIRTY  explicit override for ``dirty`` (1/true/yes/on),
                        wins over anything parsed from STAPEL_GIT_SHA
"""
import os
import platform
import sys
from importlib import metadata

import django
from django.conf import settings
from django.http import JsonResponse
from django.urls import path

# The document's shape, shared with every other thing in a fleet that answers
# this question — including the storefront's static /_version.json, which is
# emitted by a node build and has no Python in it at all. One shape means a
# walker or a deploy gate parses one thing, not one per service kind.
SCHEMA = "stapel.version/1"

# Which distributions to report. Every library a stapel fleet is assembled from
# is named `stapel-*`; a third-party pin is a rebuild artefact and listing all
# of site-packages would turn a version answer into an inventory. Deployments
# that vendor their own libraries widen it through the setting below.
DEFAULT_LIBRARY_PREFIXES = ("stapel",)


def _config():
    return getattr(settings, "STAPEL_VERSION_ENDPOINT", None) or {}


def installed_libraries(prefixes=None):
    """``{distribution: version}`` for every installed dist matching a prefix.

    Read from the running interpreter's metadata, so this is what the process
    IMPORTED, not what someone intended it to import. Sorted, because two of
    these documents get compared by eye and by ``diff``.
    """
    if prefixes is None:
        prefixes = _config().get("LIBRARY_PREFIXES", DEFAULT_LIBRARY_PREFIXES)
    prefixes = tuple(prefixes)
    found = {}
    for dist in metadata.distributions():
        # A broken/partial dist in site-packages must not take the endpoint
        # down: this is the thing you call WHEN something is wrong.
        try:
            name = dist.metadata["Name"]
            version = dist.version
        except Exception:
            continue
        if not name:
            continue
        if name.startswith(prefixes):
            # Duplicate metadata dirs (a botched upgrade) are themselves worth
            # seeing, so the last one does not silently win — the lower one is
            # kept and the disagreement shows up as a version nobody expects.
            found.setdefault(name, version)
    return dict(sorted(found.items()))


def build_info():
    """The image identity, from the environment the deployment stamped.

    ``None`` for anything unset. An unstamped image says so; it does not
    inherit a plausible value from anywhere.

    A fleet's Makefile stamps ``STAPEL_GIT_SHA`` as ``<sha>-dirty`` when the
    tree that was built was dirty. Read raw, that suffix rides along inside
    ``commit`` and ``commit_short`` while ``dirty`` stays ``null`` — a
    build that was NOT clean reads as one with no dirty information at all,
    which a walker takes for clean. The suffix is parsed off here and turned
    into the boolean it actually is; ``commit``/``commit_short`` report only
    the sha. ``STAPEL_BUILD_DIRTY``, if the deployment sets it explicitly,
    wins over anything parsed from the sha.
    """
    def env(name):
        value = os.environ.get(name, "").strip()
        return value or None

    sha = env("STAPEL_GIT_SHA")
    dirty = False
    if sha and sha.endswith("-dirty"):
        dirty = True
        sha = sha[: -len("-dirty")] or None

    dirty_override = env("STAPEL_BUILD_DIRTY")
    if dirty_override is not None:
        dirty = dirty_override.lower() in ("1", "true", "yes", "on")

    return {
        "commit": sha,
        "commit_short": sha[:8] if sha else None,
        "dirty": dirty if sha or dirty_override is not None else None,
        "image": {"name": env("STAPEL_IMAGE_NAME"), "tag": env("STAPEL_IMAGE_TAG")},
        "built_at": env("STAPEL_BUILD_TIME"),
    }


def version_view(request):
    """``GET <prefix>api/version/`` — see the module docstring."""
    if not _config().get("PUBLIC", True):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and user.is_staff):
            # 404 and not 403: a surface that is not public should not confirm
            # that it exists to a caller that may not have it.
            return JsonResponse({"detail": "Not found."}, status=404)

    info = build_info()
    return JsonResponse(
        {
            "schema": SCHEMA,
            "service": getattr(settings, "SERVICE_NAME", "unknown"),
            **info,
            "libraries": installed_libraries(),
            "runtime": {
                "python": platform.python_version(),
                "django": django.get_version(),
                # Which interpreter answered. In a fleet where one service is
                # five containers off one image, this plus `libraries` is what
                # makes "the web process and the worker disagree" visible from
                # outside instead of only to a gate with a docker socket.
                "executable": sys.executable,
            },
        },
        # `json_dumps_params` so the answer is readable in a terminal without
        # piping it through anything — this is a curl-first endpoint.
        json_dumps_params={"indent": 2, "sort_keys": False},
    )


def get_version_urls(prefix: str = ""):
    """URL patterns for the version endpoint.

    Already included by :func:`get_health_urls`, so a service that mounts
    health has this too and there is nothing per-service to remember. Exposed
    separately for a service that wants it under a different prefix.
    """
    return [path(f"{prefix}api/version/", version_view, name="version")]
