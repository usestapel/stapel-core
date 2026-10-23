"""``manage.py run_seeds`` runs the commands named in ``settings.STAPEL_SEEDS``."""

from io import StringIO

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from stapel_core.django.management.commands.run_seeds import Command

pytestmark = pytest.mark.django_db

_APPS = [*settings.INSTALLED_APPS, "tests.seed_test_app"]


def _run(*args):
    out = StringIO()
    call_command(Command(), *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def _demo_users():
    return get_user_model().objects.filter(username__startswith="seed-demo-").count()


@override_settings(INSTALLED_APPS=_APPS, STAPEL_SEEDS=["seed_demo_users"])
def test_declared_seed_runs_and_a_second_run_changes_nothing():
    out = _run()
    assert _demo_users() == 3
    assert "run_seeds: seed_demo_users" in out
    _run()
    assert _demo_users() == 3


@override_settings(INSTALLED_APPS=_APPS, STAPEL_SEEDS=[])
def test_empty_list_runs_nothing_and_succeeds():
    assert "STAPEL_SEEDS is empty" in _run()
    assert _demo_users() == 0


@override_settings(INSTALLED_APPS=_APPS)
def test_unset_setting_is_an_empty_list():
    del settings.STAPEL_SEEDS
    assert "nothing to run" in _run()


@override_settings(INSTALLED_APPS=_APPS, STAPEL_SEEDS=["seed_demo_users", "seed_not_installed"])
def test_unknown_name_fails_naming_it_before_any_seed_runs():
    with pytest.raises(CommandError, match="seed_not_installed"):
        _run()
    assert _demo_users() == 0


@override_settings(INSTALLED_APPS=_APPS, STAPEL_SEEDS=["seed_explodes", "seed_demo_users"])
def test_failing_seed_is_named_and_stops_the_run():
    with pytest.raises(CommandError, match="seed 'seed_explodes' failed: the seed source"):
        _run()
    assert _demo_users() == 0


@override_settings(INSTALLED_APPS=_APPS, STAPEL_SEEDS="seed_demo_users")
def test_a_bare_string_is_refused():
    with pytest.raises(CommandError, match="must be a list"):
        _run()


@override_settings(INSTALLED_APPS=_APPS, STAPEL_SEEDS=["seed_demo_users"])
def test_list_prints_the_declared_seeds_without_running_them():
    assert _run("--list").split() == ["seed_demo_users"]
    assert _demo_users() == 0


def test_command_ships_in_the_common_app():
    from stapel_core.django.apps import CommonDjangoConfig

    assert CommonDjangoConfig.name == "stapel_core.django"
    assert Command.__module__ == "stapel_core.django.management.commands.run_seeds"
