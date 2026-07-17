"""§55 slice 2: swap declarations, the presenter auto-catalog (PRESENTERS.MD),
and the reference get_presenter() consumer (JWTStatusView.profile).

Covers stapel_core.django.swappable (declare_swap/declared_swaps),
stapel_core.django.api.catalog (introspection + rendering + write),
the presenter_catalog management command (write + --check freshness gate),
and the profile block JWTStatusView builds through the swappable presenter.
"""
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import RequestFactory, override_settings

from stapel_core.django.api.catalog import (
    autodiscover_presenters,
    presenter_catalog,
    render_presenters_md,
    write_presenters_md,
)
from stapel_core.django.jwt.views import JWTStatusView
from stapel_core.django.management.commands.presenter_catalog import (
    Command as CatalogCommand,
)
from stapel_core.django.swappable import (
    clear_swap_cache,
    declare_swap,
    declared_swaps,
)
from stapel_core.django.users.presenters import (
    DEFAULT_PRESENTER,
    PRESENTER_KEY,
    UserProfilePresenter,
)

factory = RequestFactory()
PROVIDER = "stapel_core.django.jwt.views.jwt_provider"


@pytest.fixture(autouse=True)
def _reset_swap_cache():
    clear_swap_cache()
    yield
    clear_swap_cache()


# ---------------------------------------------------------------------------
# declare_swap / declared_swaps
# ---------------------------------------------------------------------------


class TestSwapDeclarations:
    def test_users_presenter_declared_at_import_time(self):
        # users/presenters.py is imported (conftest installs the app) — its
        # module-level declare_swap() must already be visible, no accessor
        # call needed.
        assert declared_swaps().get(PRESENTER_KEY) == DEFAULT_PRESENTER

    def test_declare_swap_registers_key(self):
        declare_swap("CATALOG_TEST_KEY", "some.dotted.Path")
        assert declared_swaps()["CATALOG_TEST_KEY"] == "some.dotted.Path"

    def test_declare_swap_first_declaration_wins(self):
        declare_swap("CATALOG_DUP_KEY", "first.Class")
        declare_swap("CATALOG_DUP_KEY", "second.Class")
        assert declared_swaps()["CATALOG_DUP_KEY"] == "first.Class"

    def test_declarations_survive_cache_clear(self):
        declare_swap("CATALOG_SURVIVOR", "x.Y")
        clear_swap_cache()
        assert "CATALOG_SURVIVOR" in declared_swaps()

    def test_declared_swaps_returns_a_copy(self):
        snapshot = declared_swaps()
        snapshot["INJECTED"] = "nope"
        assert "INJECTED" not in declared_swaps()


# ---------------------------------------------------------------------------
# catalog introspection
# ---------------------------------------------------------------------------


class TestPresenterCatalog:
    def test_autodiscover_imports_app_presenter_modules(self):
        # users app ships a presenters module; autodiscover must count it
        # and must not raise on apps without one (outbox, taskstore, ...).
        assert autodiscover_presenters() >= 1

    def test_users_pilot_entry(self):
        entries = presenter_catalog()
        by_presenter = {e.presenter: e for e in entries}
        entry = by_presenter[DEFAULT_PRESENTER]
        assert entry.swap_key == PRESENTER_KEY
        assert entry.model == "users.User"
        assert entry.dto == "UserProfilePresenterDTO"
        fields = {f.name: f for f in entry.fields}
        assert set(fields) == {"id", "email", "display_name"}
        assert fields["display_name"].source == "computed"
        assert "display name" in fields["display_name"].description.lower()
        assert fields["email"].source == "email"

    def test_presenter_without_declared_key_has_none(self):
        entries = presenter_catalog()
        # Test-local presenters (OutboxPresenter etc. from test_presenters.py,
        # if imported) and any undeclared ones must not crash the catalog —
        # swap_key is simply None for them.
        for e in entries:
            if e.presenter != DEFAULT_PRESENTER:
                assert e.swap_key is None or e.swap_key in declared_swaps()


# ---------------------------------------------------------------------------
# rendering + write
# ---------------------------------------------------------------------------


class TestRenderPresentersMd:
    def test_render_contains_swap_table_and_field_rows(self):
        entries = presenter_catalog()
        text = render_presenters_md(entries)
        assert "## Swap points (`STAPEL_SWAP`)" in text
        assert f"| `{PRESENTER_KEY}` | `{DEFAULT_PRESENTER}` | presenter |" in text
        assert f"### `{DEFAULT_PRESENTER}`" in text
        assert "- **Model:** `users.User`" in text
        assert "- **DTO:** `UserProfilePresenterDTO`" in text
        assert "| `display_name` | `str` | computed |" in text

    def test_render_empty_catalog(self):
        text = render_presenters_md([], swaps={})
        assert "*(no swap points declared)*" in text
        assert "*(no presenters registered)*" in text

    def test_write_presenters_md(self, tmp_path):
        out = tmp_path / "PRESENTERS.MD"
        written = write_presenters_md(out)
        assert written == out
        assert PRESENTER_KEY in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# management command: write + --check freshness gate
# ---------------------------------------------------------------------------


class TestPresenterCatalogCommand:
    def test_command_writes_file(self, tmp_path):
        out = tmp_path / "PRESENTERS.MD"
        stdout = StringIO()
        call_command(CatalogCommand(), "--out", str(out), stdout=stdout)
        assert out.is_file()
        assert "wrote" in stdout.getvalue()
        assert PRESENTER_KEY in out.read_text(encoding="utf-8")

    def test_check_passes_on_fresh_file(self, tmp_path):
        out = tmp_path / "PRESENTERS.MD"
        call_command(CatalogCommand(), "--out", str(out), stdout=StringIO())
        stdout = StringIO()
        call_command(CatalogCommand(), "--out", str(out), "--check", stdout=stdout)
        assert "fresh" in stdout.getvalue()

    def test_check_fails_on_stale_file(self, tmp_path):
        out = tmp_path / "PRESENTERS.MD"
        out.write_text("# stale by hand\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            call_command(CatalogCommand(), "--out", str(out), "--check", stdout=StringIO())
        assert exc.value.code == 1

    def test_check_fails_on_missing_file(self, tmp_path):
        out = tmp_path / "MISSING.MD"
        with pytest.raises(SystemExit):
            call_command(CatalogCommand(), "--out", str(out), "--check", stdout=StringIO())


# ---------------------------------------------------------------------------
# reference consumer: JWTStatusView.profile through get_presenter()
# ---------------------------------------------------------------------------


class SwappedStatusPresenter(UserProfilePresenter):
    """Host presenter used to prove the status endpoint honors the swap.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "email": "user@example.com",
            "display_name": "Alice",
            "shouting_name": "ALICE"
        }
    """

    model = UserProfilePresenter.model
    fields = UserProfilePresenter.fields
    custom_fields = {
        **UserProfilePresenter.custom_fields,
        "shouting_name": type(UserProfilePresenter.custom_fields["display_name"])(
            type=str,
            source=lambda dao: dao.username.upper(),
            help_text="Host-flavored shouting name.",
        ),
    }


def _status_request(user):
    req = factory.get("/status/")
    req.COOKIES = {"stapel_jwt": "acc.tok"}
    req.session = MagicMock()
    req.user = user
    return req


def _auth_user(**kwargs):
    defaults = dict(
        is_authenticated=True,
        id="uid-9",
        email="p@example.com",
        username="presented",
        is_staff=False,
        is_superuser=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestStatusViewProfileBlock:
    def test_profile_is_presented_dto(self):
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.return_value = {"exp": 1}
            resp = JWTStatusView.as_view()(_status_request(_auth_user()))
        body = json.loads(resp.content)
        assert body["profile"] == {
            "id": "uid-9",
            "email": "p@example.com",
            "display_name": "presented",
        }
        # the legacy flat ``user`` block is gone from the wire
        assert "user" not in body

    def test_profile_none_for_anonymous(self):
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.return_value = None
            resp = JWTStatusView.as_view()(
                _status_request(SimpleNamespace(is_authenticated=False))
            )
        body = json.loads(resp.content)
        assert body["profile"] is None

    def test_profile_honors_stapel_swap(self):
        with override_settings(
            STAPEL_SWAP={PRESENTER_KEY: f"{__name__}.SwappedStatusPresenter"}
        ):
            clear_swap_cache()
            with patch(PROVIDER) as provider:
                provider.handler.decode_token.return_value = {"exp": 1}
                resp = JWTStatusView.as_view()(_status_request(_auth_user()))
        body = json.loads(resp.content)
        assert body["profile"]["shouting_name"] == "PRESENTED"
        assert body["profile"]["display_name"] == "presented"
