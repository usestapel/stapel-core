"""A named posture is values PLUS the check that keeps them true.

The case that motivated the artifact: a client's stand carried its posture as
a bespoke settings tier that re-read the mock-OTP flags from the environment,
defaulting them ON, over a production layer that pinned them off — and
silenced the two auth checks that report exactly that. Nothing named the
posture, so nothing contradicted it. These tests are mostly about the
contradiction: the drift finding must be RED before it is believed.
"""
import os

import pytest
from django.core import checks
from django.test import override_settings

from stapel_core.django.check_guard import (
    SecurityCriticalError,
    is_security_critical,
    security_critical_ids,
)
from stapel_core.django.presets import (
    E001_POSTURE_VALUE_OVERRIDDEN,
    E002_BAD_POSTURE_DECLARATION,
    POSTURE_SETTING,
    PRESETS,
    PROTOTYPE_STAGE_NOTE,
    RETIRED_ENV_SETTING,
    W001_POSTURE_VALUE_DIFFERS,
    W002_RETIRED_ENV_SET,
    W003_PROTOTYPE_STAGE_IDLE,
    PresetValue,
    check_posture_coherence,
    declared_posture,
    posture_spec,
    private_space,
    public_space,
    stage,
    stage_finding,
)


def ids_of(findings):
    return [f.id for f in findings]


def spread(preset):
    """What a settings module does with a preset, as a settings override."""
    return {key: value for key, value in preset.items()}


# ---------------------------------------------------------------------------
# The values
# ---------------------------------------------------------------------------


def test_private_ships_registration_closed_and_no_street_mandate():
    preset = private_space()
    assert preset["STAPEL_WORKSPACES"]["STREET_LANDING_MODE"] == "none"
    assert not any(
        value for key, value in preset["STAPEL_AUTH"].items()
        if key.endswith("_REGISTRATION")
    )


def test_the_requests_door_is_one_explicit_method_not_a_second_default():
    door = private_space(door="requests")["STAPEL_AUTH"]
    assert door["AUTH_EMAIL_REGISTRATION"] is True
    assert door["AUTH_PHONE_REGISTRATION"] is False
    assert door["AUTH_OAUTH_REGISTRATION"] is False
    assert door["AUTH_SSO_REGISTRATION"] is False
    # And it is visible in the deployment's own settings file, as a value.
    assert private_space()["STAPEL_AUTH"]["AUTH_EMAIL_REGISTRATION"] is False


def test_public_ships_registration_open_and_a_personal_landing():
    preset = public_space()
    assert preset["STAPEL_WORKSPACES"]["STREET_LANDING_MODE"] == "personal"
    assert preset["STAPEL_AUTH"]["AUTH_EMAIL_REGISTRATION"] is True


def test_both_postures_pin_mock_one_time_codes_off():
    """The sandbox relaxation that is not portable: a fixed pin accepted for
    any address authenticates as an existing owner, in every posture."""
    for preset in (private_space(), private_space(door="requests"), public_space()):
        assert preset["STAPEL_AUTH"]["USE_MOCK_SMS_OTP"] is False
        assert preset["STAPEL_AUTH"]["USE_MOCK_EMAIL_OTP"] is False


def test_an_unknown_door_is_refused_at_the_settings_line_that_names_it():
    with pytest.raises(ValueError):
        private_space(door="open")


def test_every_key_carries_its_reason():
    """A preset of settings-just-in-case is a design document in Python."""
    for name in PRESETS:
        options = {"door": "requests"} if name == "private_space" else {}
        for entries in posture_spec(name, **options).values():
            for key, item in entries.items():
                assert isinstance(item, PresetValue), key
                assert item.why.strip(), key


def test_the_manifest_records_the_name_and_options_never_the_values():
    """A manifest carrying values could be edited into agreement with a
    drifted setting; the check re-derives instead."""
    manifest = private_space(door="requests")[POSTURE_SETTING]
    assert manifest == {
        "PRESET": "private_space",
        "OPTIONS": {"door": "requests", "stage": "live"},
    }


def test_a_preset_imports_no_module_and_returns_only_keys():
    """Why this lives in the core: composition of keys, not of code."""
    import stapel_core.django.presets as module

    source = open(module.__file__).read()
    assert "import stapel_auth" not in source
    assert "import stapel_workspaces" not in source


# ---------------------------------------------------------------------------
# The check — red before believed
# ---------------------------------------------------------------------------


def test_no_posture_declared_is_not_incoherent():
    assert declared_posture() is None
    assert check_posture_coherence() == []


def test_a_spread_preset_is_green():
    with override_settings(**spread(private_space(door="requests"))):
        assert check_posture_coherence() == []


def test_the_case_that_matters_a_quiet_security_override_is_red():
    """The project spreads the private preset and then overrides one line."""
    preset = private_space(door="requests")
    settings_dict = spread(preset)
    settings_dict["STAPEL_AUTH"] = {
        **preset["STAPEL_AUTH"],
        "USE_MOCK_EMAIL_OTP": True,  # the line below the spread
    }
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
    assert ids_of(findings) == [E001_POSTURE_VALUE_OVERRIDDEN]
    assert isinstance(findings[0], SecurityCriticalError)
    assert "USE_MOCK_EMAIL_OTP" in findings[0].msg


def test_the_security_finding_survives_a_blanket_silencing_line():
    """SILENCED_SYSTEM_CHECKS is the route the old sandbox tier used."""
    preset = private_space()
    settings_dict = spread(preset)
    settings_dict["STAPEL_AUTH"] = {**preset["STAPEL_AUTH"], "USE_MOCK_SMS_OTP": True}
    settings_dict["SILENCED_SYSTEM_CHECKS"] = [E001_POSTURE_VALUE_OVERRIDDEN]
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
        assert [f for f in findings if not f.is_silenced()]


def test_the_only_route_to_quiet_is_a_waiver_that_states_a_reason():
    """The sanctioned exception (a private cloud fronted by an IdP) is loud."""
    preset = private_space()
    settings_dict = spread(preset)
    settings_dict["STAPEL_AUTH"] = {
        **preset["STAPEL_AUTH"], "AUTH_SSO_REGISTRATION": True,
    }
    settings_dict["STAPEL_SECURITY_CHECK_WAIVERS"] = {
        E001_POSTURE_VALUE_OVERRIDDEN: "corporate IdP owns entry here",
    }
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
        assert ids_of(findings) == [E001_POSTURE_VALUE_OVERRIDDEN]
        assert findings[0].is_silenced()  # waived, and W002 announces it


def test_reopening_the_street_mandate_is_a_security_finding():
    preset = private_space()
    settings_dict = spread(preset)
    settings_dict["STAPEL_WORKSPACES"] = {"STREET_LANDING_MODE": "personal"}
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
    assert ids_of(findings) == [E001_POSTURE_VALUE_OVERRIDDEN]
    assert "STREET_LANDING_MODE" in findings[0].msg


def test_a_namespace_that_was_never_spread_is_the_same_finding():
    """Values that never arrived are drift too — the posture is not in effect."""
    preset = private_space()
    settings_dict = spread(preset)
    settings_dict.pop("STAPEL_WORKSPACES")
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
    assert ids_of(findings) == [E001_POSTURE_VALUE_OVERRIDDEN]
    assert "not set at all" in findings[0].msg


def test_a_non_security_difference_is_visible_but_not_a_blocker():
    preset = public_space()
    settings_dict = spread(preset)
    settings_dict["STAPEL_WORKSPACES"] = {"STREET_LANDING_MODE": "none"}
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
    assert ids_of(findings) == [W001_POSTURE_VALUE_DIFFERS]


def test_the_check_reads_the_running_namespace_not_only_the_literal_dict(monkeypatch):
    """An AppSettings namespace applies defaults and env layering; a posture an
    environment variable could undo is not a posture, so the effective value is
    what gets compared whenever the owning module runs in this process."""
    import stapel_core.conf as conf

    class LiveNamespace:
        namespace = "STAPEL_AUTH"

        def __getattr__(self, key):
            return True  # as if the environment had reopened every gate

    monkeypatch.setattr(conf, "registered_settings", lambda: [LiveNamespace()])
    settings_dict = spread(private_space(door="requests"))
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
    assert set(ids_of(findings)) == {E001_POSTURE_VALUE_OVERRIDDEN}


def test_a_hand_written_manifest_that_names_nothing_is_refused():
    with override_settings(**{POSTURE_SETTING: {"PRESET": "no_such_space"}}):
        assert ids_of(check_posture_coherence()) == [E002_BAD_POSTURE_DECLARATION]
    with override_settings(**{POSTURE_SETTING: "private_space"}):
        assert ids_of(check_posture_coherence()) == [E002_BAD_POSTURE_DECLARATION]
    with override_settings(**{
        POSTURE_SETTING: {"PRESET": "private_space", "OPTIONS": {"door": "open"}},
    }):
        assert ids_of(check_posture_coherence()) == [E002_BAD_POSTURE_DECLARATION]


def test_the_id_is_declared_security_critical_at_its_constant():
    assert is_security_critical(E001_POSTURE_VALUE_OVERRIDDEN)
    assert "registration doors" in security_critical_ids()[
        E001_POSTURE_VALUE_OVERRIDDEN
    ]


# ---------------------------------------------------------------------------
# Retired environment variables
# ---------------------------------------------------------------------------


def test_a_retired_variable_still_set_on_the_stand_is_reported_by_name():
    os.environ["AUTH_USE_MOCK_EMAIL_OTP"] = "true"
    try:
        with override_settings(**{
            RETIRED_ENV_SETTING: {
                "AUTH_USE_MOCK_EMAIL_OTP": "the posture pins mock OTP off",
                "AUTH_NEVER_SET_ANYWHERE": "likewise",
            },
        }):
            findings = check_posture_coherence()
    finally:
        os.environ.pop("AUTH_USE_MOCK_EMAIL_OTP", None)
    assert ids_of(findings) == [W002_RETIRED_ENV_SET]
    assert "AUTH_USE_MOCK_EMAIL_OTP" in findings[0].msg
    assert "true" not in findings[0].msg  # the name, never the value


def test_registered_under_its_own_tag():
    assert "stapel_presets" in checks.registry.registry.tags_available()
    assert check_posture_coherence in checks.registry.registry.get_checks()
    assert tuple(check_posture_coherence.tags) == ("stapel_presets",)


# ---------------------------------------------------------------------------
# Stage: production, and not yet launched
# ---------------------------------------------------------------------------

#: The live spread, captured before the stage option existed. Values only —
#: the manifest gained the option and is asserted separately. A stage that
#: moved one of these would be a change to every deployment already running.
LIVE_VALUES_BEFORE_THE_STAGE_OPTION = {
    "private_space": {
        "STAPEL_AUTH": {
            "AUTH_EMAIL_REGISTRATION": False,
            "AUTH_OAUTH_REGISTRATION": False,
            "AUTH_PASSWORD_REGISTRATION": False,
            "AUTH_PHONE_REGISTRATION": False,
            "AUTH_SSO_REGISTRATION": False,
            "USE_MOCK_EMAIL_OTP": False,
            "USE_MOCK_SMS_OTP": False,
        },
        "STAPEL_WORKSPACES": {"STREET_LANDING_MODE": "none"},
    },
    "public_space": {
        "STAPEL_AUTH": {
            "AUTH_EMAIL_REGISTRATION": True,
            "AUTH_OAUTH_REGISTRATION": True,
            "AUTH_PASSWORD_REGISTRATION": False,
            "AUTH_PHONE_REGISTRATION": True,
            "AUTH_SSO_REGISTRATION": True,
            "USE_MOCK_EMAIL_OTP": False,
            "USE_MOCK_SMS_OTP": False,
        },
        "STAPEL_WORKSPACES": {"STREET_LANDING_MODE": "personal"},
    },
}


def values_of(preset):
    return {key: value for key, value in preset.items() if key != POSTURE_SETTING}


def test_the_live_spread_is_what_it_was_before_the_option_existed():
    """The default is not a new posture: consumers already on these presets
    keep exactly the values they run today."""
    assert values_of(private_space()) == LIVE_VALUES_BEFORE_THE_STAGE_OPTION[
        "private_space"
    ]
    assert values_of(public_space()) == LIVE_VALUES_BEFORE_THE_STAGE_OPTION[
        "public_space"
    ]
    assert values_of(private_space(stage="live")) == values_of(private_space())
    assert values_of(public_space(stage="live")) == values_of(public_space())


def test_an_unknown_stage_is_refused_at_the_settings_line_that_names_it():
    with pytest.raises(ValueError):
        private_space(stage="staging")
    with pytest.raises(ValueError):
        public_space(stage="demo")
    with pytest.raises(ValueError):
        posture_spec("public_space", stage="")


def test_the_prototype_spread_says_nothing_about_the_mock_keys():
    """Not pinned ON — absent. The deployment decides, and the coherence
    check has nothing to compare, which is what makes the stage honest."""
    for preset in (private_space(stage="prototype"), public_space(stage="prototype")):
        assert "USE_MOCK_SMS_OTP" not in preset["STAPEL_AUTH"]
        assert "USE_MOCK_EMAIL_OTP" not in preset["STAPEL_AUTH"]
    # Everything else the posture says is unchanged by the stage.
    prototype = private_space(door="requests", stage="prototype")
    live = private_space(door="requests")
    assert prototype["STAPEL_WORKSPACES"] == live["STAPEL_WORKSPACES"]
    assert prototype["STAPEL_AUTH"] == {
        key: value for key, value in live["STAPEL_AUTH"].items()
        if not key.startswith("USE_MOCK_")
    }


def test_the_stage_is_recorded_in_the_manifest_like_the_door_is():
    assert public_space(stage="prototype")[POSTURE_SETTING] == {
        "PRESET": "public_space", "OPTIONS": {"stage": "prototype"},
    }


def test_mock_codes_on_a_prototype_stand_are_not_drift():
    """The case the ruling is about: a public stand running mock OTP before
    launch, with no SILENCED_SYSTEM_CHECKS line anywhere."""
    preset = private_space(door="requests", stage="prototype")
    settings_dict = spread(preset)
    settings_dict["STAPEL_AUTH"] = {
        **preset["STAPEL_AUTH"],
        "USE_MOCK_EMAIL_OTP": True,
        "USE_MOCK_SMS_OTP": True,
    }
    with override_settings(**settings_dict):
        assert check_posture_coherence() == []


def test_the_same_stand_declared_live_is_the_security_finding_it_was():
    preset = private_space(door="requests")
    settings_dict = spread(preset)
    settings_dict["STAPEL_AUTH"] = {
        **preset["STAPEL_AUTH"], "USE_MOCK_EMAIL_OTP": True,
    }
    with override_settings(**settings_dict):
        findings = check_posture_coherence()
    assert ids_of(findings) == [E001_POSTURE_VALUE_OVERRIDDEN]


def test_the_declared_stage_is_readable_on_its_own():
    assert stage() is None  # nothing declared
    with override_settings(**spread(public_space())):
        assert stage() == "live"
    with override_settings(**spread(public_space(stage="prototype"))):
        assert stage() == "prototype"


def test_a_manifest_without_the_option_reads_as_live():
    """A declaration that predates the option cannot claim to be a prototype."""
    with override_settings(**{
        POSTURE_SETTING: {"PRESET": "public_space", "OPTIONS": {}},
    }):
        assert stage() == "live"


def _stub_channel_error():
    return checks.Error(
        "USE_MOCK_EMAIL_OTP is enabled with DEBUG=False.",
        hint="Configure a real email provider.",
        id="stapel_auth.E001",
    )


def test_a_stub_channel_error_is_a_warning_that_says_why_in_the_prototype_stage():
    with override_settings(**spread(public_space(stage="prototype"))):
        reported = stage_finding(_stub_channel_error(), warning_id="stapel_auth.W011")
    assert isinstance(reported, checks.Warning)
    assert reported.id == "stapel_auth.W011"
    assert reported.msg == (
        "USE_MOCK_EMAIL_OTP is enabled with DEBUG=False. " + PROTOTYPE_STAGE_NOTE
    )
    assert reported.hint == "Configure a real email provider."
    assert 'stage="live"' in reported.msg


def test_the_same_error_is_untouched_when_live_or_undeclared():
    original = _stub_channel_error()
    assert stage_finding(original, warning_id="stapel_auth.W011") is original
    with override_settings(**spread(public_space())):
        assert stage_finding(original, warning_id="stapel_auth.W011") is original


def test_a_prototype_stage_with_nothing_stubbed_is_a_visibility_warning():
    """The stage must not outlive its reason."""
    with override_settings(
        **spread(public_space(stage="prototype")),
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
    ):
        assert ids_of(check_posture_coherence()) == [W003_PROTOTYPE_STAGE_IDLE]


def test_a_prototype_stage_over_a_real_stub_is_quiet():
    preset = public_space(stage="prototype")
    settings_dict = spread(preset)
    settings_dict["STAPEL_AUTH"] = {**preset["STAPEL_AUTH"], "USE_MOCK_SMS_OTP": True}
    with override_settings(
        **settings_dict,
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
    ):
        assert check_posture_coherence() == []
    # A console email backend is prototypical too, with no mock OTP at all.
    with override_settings(
        **spread(preset),
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    ):
        assert check_posture_coherence() == []


def test_a_live_stage_never_asks_what_is_stubbed():
    with override_settings(
        **spread(public_space()),
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
    ):
        assert ids_of(check_posture_coherence()) == []
