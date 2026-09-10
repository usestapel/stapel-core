"""Host classification: one answer to "is this reachable from outside?".

The question a check asks before it decides whether a relaxation is still
somebody's laptop. It used to be answered privately inside stapel-auth, where
only one check could reach it.
"""
from stapel_core.django.hosts import LOCAL_HOSTS, looks_public


def test_the_names_a_developer_machine_answers_to_are_not_public():
    for host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver", ""):
        assert looks_public(host) is False, host
    assert set(LOCAL_HOSTS) >= {"localhost", "127.0.0.1"}


def test_a_real_domain_is_public():
    for host in ("stand.example.com", "example.com", "8.8.8.8"):
        assert looks_public(host) is True, host


def test_the_shapes_a_host_arrives_in_do_not_change_the_answer():
    """Case, surrounding space and the fully-qualified trailing dot."""
    assert looks_public("  LOCALHOST.  ") is False
    assert looks_public("Stand.Example.Com.") is True
    assert looks_public(None) is False


def test_development_and_private_ranges_are_local():
    for host in ("app.local", "app.localhost", "192.168.1.9", "10.0.0.4", "172.17.0.2"):
        assert looks_public(host) is False, host


def test_a_wildcard_is_not_classified_here():
    """``*`` is not a host name; what it means is the caller's finding to
    make, and for mock credentials it counts as public."""
    assert looks_public("*") is True
