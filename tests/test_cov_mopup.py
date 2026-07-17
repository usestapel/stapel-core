"""Mop-up coverage: __str__ reprs and tiny config branches."""
from unittest.mock import patch

import pytest
from django.utils import timezone


def test_outbox_event_str_both_states():
    from stapel_core.django.outbox.models import OutboxEvent

    row = OutboxEvent(topic="user.deleted", event_json="{}", attempts=2)
    assert str(row) == "user.deleted [pending(2)]"
    row.dispatched_at = timezone.now()
    assert str(row) == "user.deleted [dispatched]"


def test_task_record_str():
    from stapel_core.django.taskstore.models import TaskRecord

    record = TaskRecord(kind="llm.summarize", state=TaskRecord.RUNNING)
    assert str(record) == "llm.summarize [running]"


def test_validation_enabled_follows_debug_when_unset(settings):
    from stapel_core.comm.config import validation_enabled

    settings.STAPEL_COMM = {}
    settings.DEBUG = False
    assert validation_enabled() is False
    settings.DEBUG = True
    assert validation_enabled() is True


def test_appsettings_survives_missing_setting_changed_signal():
    # Poison the import used by _connect_reload — the constructor must
    # swallow it (Django-not-ready path) and stay functional.
    with patch.dict("sys.modules", {"django.test.signals": None}):
        from stapel_core.conf import AppSettings

        s = AppSettings("STAPEL_MOPUP", defaults={"KEY": "v"})
    assert s.KEY == "v"


def test_exception_handler_validation_error_without_messages():
    from django.core.exceptions import ValidationError as DjangoValidationError

    from stapel_core.django.api.errors import stapel_exception_handler

    class NoMessages(DjangoValidationError):
        @property
        def message_dict(self):
            raise AttributeError("message_dict")

        @property
        def messages(self):
            raise AttributeError("messages")

    response = stapel_exception_handler(NoMessages("boom"), context={})
    assert response.status_code == 400
    assert response.data["params"]["detail"] == ["boom"]


def test_oauth_provider_abstract_body():
    from stapel_core.oauth import OAuthProvider

    class Impl(OAuthProvider):
        def get_user_data(self, access_token):
            # exercise the abstract default body
            return super().get_user_data(access_token)

    with pytest.raises(TypeError):
        OAuthProvider()  # still abstract

    assert Impl().get_user_data("token") is None
