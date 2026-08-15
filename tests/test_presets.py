"""A named posture is values PLUS the check that keeps them true.

The case that motivated the artifact: meettoday's stand carried its posture as
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
    RETIRED_ENV_SETTING,
    W001_POSTURE_VALUE_DIFFERS,
    W002_RETIRED_ENV_SET,
    PresetValue,
    check_posture_coherence,
    declared_posture,
    posture_spec,
    private_space,
    public_space,
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
    assert manifest == {"PRESET": "private_space", "OPTIONS": {"door": "requests"}}


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
