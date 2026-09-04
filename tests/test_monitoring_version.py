"""django/monitoring/version.py — "which build is this process running?"

The point of every test here is that the answer is READ, never declared: an
unstamped image must say so rather than inherit a plausible value, and the
library list must come from what is installed rather than from anything a
human typed.
"""
import json

import pytest
from django.test import RequestFactory, override_settings

from stapel_core.django.monitoring import health as health_mod
from stapel_core.django.monitoring import version as version_mod


@pytest.fixture
def rf():
    return RequestFactory()


def _body(response):
    return json.loads(response.content)


# ---------------------------------------------------------------------------
# installed_libraries — read from the interpreter, not from a manifest
# ---------------------------------------------------------------------------


def test_reports_the_installed_version_of_stapel_core_itself():
    """The one distribution guaranteed present is the one under test."""
    libs = version_mod.installed_libraries()
    assert "stapel-core" in libs
    # Matched against the metadata rather than against pyproject.toml's literal:
    # asserting a hardcoded number here would be a second place for the version
    # to live, which is the defect this module exists to remove.
    from importlib import metadata

    assert libs["stapel-core"] == metadata.version("stapel-core")


def test_the_prefix_decides_what_is_reported():
    # `pytest` is installed in this environment and is not a stapel library, so
    # its absence under the default prefix and presence under its own prove the
    # filter is doing the filtering.
    assert "pytest" not in version_mod.installed_libraries()
    assert "pytest" in version_mod.installed_libraries(prefixes=("pytest",))


def test_a_deployment_can_widen_the_prefixes():
    with override_settings(STAPEL_VERSION_ENDPOINT={"LIBRARY_PREFIXES": ["pytest"]}):
        assert "pytest" in version_mod.installed_libraries()


def test_a_broken_distribution_does_not_take_the_endpoint_down(monkeypatch):
    """This is what you call WHEN something is wrong."""

    class _Broken:
        @property
        def metadata(self):
            raise RuntimeError("unreadable METADATA")

    real = list(version_mod.metadata.distributions())
    monkeypatch.setattr(
        version_mod.metadata, "distributions", lambda *a, **k: iter([_Broken(), *real])
    )
    assert "stapel-core" in version_mod.installed_libraries()


def test_the_result_is_sorted():
    libs = version_mod.installed_libraries(prefixes=("s", "p", "d"))
    assert list(libs) == sorted(libs)


# ---------------------------------------------------------------------------
# build_info — absent is said as absent
# ---------------------------------------------------------------------------


def test_an_unstamped_image_reports_nulls_and_not_a_guess(monkeypatch):
    for name in (
        "STAPEL_GIT_SHA",
        "STAPEL_IMAGE_NAME",
        "STAPEL_IMAGE_TAG",
        "STAPEL_BUILD_TIME",
    ):
        monkeypatch.delenv(name, raising=False)
    info = version_mod.build_info()
    assert info == {
        "commit": None,
        "commit_short": None,
        "dirty": None,
        "image": {"name": None, "tag": None},
        "built_at": None,
    }


def test_an_empty_env_var_is_absent_not_empty_string(monkeypatch):
    """A Dockerfile that declares `ARG GIT_SHA=` and is never passed one sets
    the variable to "". That is "nobody told me", not "the commit is ''"."""
    monkeypatch.setenv("STAPEL_GIT_SHA", "   ")
    assert version_mod.build_info()["commit"] is None


def test_the_short_commit_is_derived_and_not_supplied(monkeypatch):
    monkeypatch.setenv("STAPEL_GIT_SHA", "2bdb78983919da1ae4a5168e196d19a8c1c338e4")
    info = version_mod.build_info()
    assert info["commit_short"] == "2bdb7898"
    assert info["commit"].startswith(info["commit_short"])


# ---------------------------------------------------------------------------
# build_info — a fleet Makefile's "-dirty" stamp, and the explicit override
# ---------------------------------------------------------------------------


def test_a_clean_sha_reports_dirty_false(monkeypatch):
    """A stamped, non-suffixed sha is a positive "this was clean", not an
    absence — it must not read as null."""
    monkeypatch.setenv("STAPEL_GIT_SHA", "2bdb78983919da1ae4a5168e196d19a8c1c338e4")
    info = version_mod.build_info()
    assert info["dirty"] is False
    assert info["commit"] == "2bdb78983919da1ae4a5168e196d19a8c1c338e4"
    assert info["commit_short"] == "2bdb7898"


def test_a_dirty_suffixed_sha_is_parsed_off(monkeypatch):
    """The fleet Makefile stamps `<sha>-dirty` when the tree that was built
    was dirty. That must become `dirty: true`, not ride along inside
    `commit`/`commit_short` where a reader takes it for clean."""
    monkeypatch.setenv(
        "STAPEL_GIT_SHA", "2bdb78983919da1ae4a5168e196d19a8c1c338e4-dirty"
    )
    info = version_mod.build_info()
    assert info["dirty"] is True
    assert info["commit"] == "2bdb78983919da1ae4a5168e196d19a8c1c338e4"
    assert info["commit_short"] == "2bdb7898"
    assert not info["commit"].endswith("-dirty")


def test_stapel_build_dirty_env_overrides_the_parsed_suffix(monkeypatch):
    """An explicit override wins, in both directions."""
    monkeypatch.setenv(
        "STAPEL_GIT_SHA", "2bdb78983919da1ae4a5168e196d19a8c1c338e4-dirty"
    )
    monkeypatch.setenv("STAPEL_BUILD_DIRTY", "false")
    assert version_mod.build_info()["dirty"] is False

    monkeypatch.setenv("STAPEL_GIT_SHA", "2bdb78983919da1ae4a5168e196d19a8c1c338e4")
    monkeypatch.setenv("STAPEL_BUILD_DIRTY", "true")
    assert version_mod.build_info()["dirty"] is True


# ---------------------------------------------------------------------------
# the view
# ---------------------------------------------------------------------------


def test_the_view_answers_the_walkers_question(rf, monkeypatch):
    monkeypatch.setenv("STAPEL_GIT_SHA", "2bdb78983919da1ae4a5168e196d19a8c1c338e4")
    monkeypatch.setenv("STAPEL_IMAGE_TAG", "sha-2bdb7898")
    monkeypatch.setenv("STAPEL_IMAGE_NAME", "svc-auth")
    monkeypatch.setenv("STAPEL_BUILD_TIME", "2026-09-03T04:00:00Z")

    with override_settings(SERVICE_NAME="Auth Service"):
        body = _body(version_mod.version_view(rf.get("/auth/api/version/")))

    assert body["schema"] == "stapel.version/1"
    assert body["service"] == "Auth Service"
    assert body["commit_short"] == "2bdb7898"
    assert body["image"] == {"name": "svc-auth", "tag": "sha-2bdb7898"}
    assert body["built_at"] == "2026-09-03T04:00:00Z"
    # The field that actually settles "is the fix in?"
    assert "stapel-core" in body["libraries"]
    assert body["runtime"]["django"]


def test_it_is_public_by_default(rf):
    assert version_mod.version_view(rf.get("/api/version/")).status_code == 200


def test_a_deployment_can_close_it_to_staff(rf):
    class _Anon:
        is_authenticated = False
        is_staff = False

    class _Staff:
        is_authenticated = True
        is_staff = True

    with override_settings(STAPEL_VERSION_ENDPOINT={"PUBLIC": False}):
        anon = rf.get("/api/version/")
        anon.user = _Anon()
        # 404 rather than 403: a closed surface does not confirm it exists.
        assert version_mod.version_view(anon).status_code == 404

        staff = rf.get("/api/version/")
        staff.user = _Staff()
        assert version_mod.version_view(staff).status_code == 200


def test_a_closed_endpoint_with_no_auth_middleware_still_refuses(rf):
    """No `request.user` at all must not read as "allowed"."""
    with override_settings(STAPEL_VERSION_ENDPOINT={"PUBLIC": False}):
        assert version_mod.version_view(rf.get("/api/version/")).status_code == 404


# ---------------------------------------------------------------------------
# the mount — nothing per-service to remember
# ---------------------------------------------------------------------------


def test_every_service_that_mounts_health_also_mounts_version():
    """The reason this rides along: an endpoint each service has to remember
    to wire is one that some service will not have wired on the day it is
    needed."""
    routes = {p.pattern._route for p in health_mod.get_health_urls("myservice/")}
    assert "myservice/api/version/" in routes
    assert "myservice/api/health/" in routes


def test_the_prefix_reaches_the_version_route():
    routes = {p.pattern._route for p in version_mod.get_version_urls("auth/")}
    assert routes == {"auth/api/version/"}
