"""Reference Gherkin bundles for the three stapel-auth flows (drift gate).

flow-system.md §3: the committed ``docs/examples/auth-flow-features/{en,ru}/``
bundles must be exactly what the Gherkin projection produces from the committed
snapshot (``source/flows.json`` + ``source/translations/flows.{en,ru}.json``,
lifted from stapel-auth) — the same byte-stable regenerate-and-diff discipline
as the SA-doc trees (stapel-auth ``tests/test_flow_docs.py``).

Regenerate after changing the generator or refreshing the snapshot:

    STAPEL_REGEN_FLOW_FEATURES=1 python -m pytest \
        tests/test_flow_feature_reference.py::test_reference_bundles_have_no_drift

then commit ``docs/examples/auth-flow-features/``. Without the env var the same
test is the CI drift gate: it regenerates into a temp dir and asserts
byte-for-byte equality with the committed bundles (a no-op regen is a no-op
diff).

The generation path here is the flows.json one (``load_flows_json``) — no
Django instance, endpoints come from the snapshot's recorded bindings; the
parity test in ``test_flow_gherkin.py`` proves it renders byte-identical to
the live-registry path.
"""
import json
import os
from pathlib import Path

from stapel_core.flows import (
    load_flows_json,
    resolve_flow_texts,
    write_language_bundle,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "docs" / "examples" / \
    "auth-flow-features"
SOURCE = EXAMPLE / "source"
LANGUAGES = ("en", "ru")
FLOW_IDS = ("auth.password_login", "auth.passwordless_login",
            "auth.step_up_verification")


def _generate(out: Path) -> None:
    flows, index = load_flows_json(json.loads((SOURCE / "flows.json").read_text()))
    for lang in LANGUAGES:
        texts = resolve_flow_texts(
            flows, lang, use_translate_function=False, catalog_dirs=[SOURCE],
        )
        write_language_bundle(flows, index, out / lang, lang, texts)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for lang in LANGUAGES
        for p in (root / lang).rglob("*")
        if p.is_file()
    }


def test_reference_bundles_have_no_drift(tmp_path):
    if os.environ.get("STAPEL_REGEN_FLOW_FEATURES"):
        _generate(EXAMPLE)
        return

    out = tmp_path / "features"
    _generate(out)
    generated = _tree(out)
    committed = _tree(EXAMPLE)

    assert set(committed) == set(generated), (
        "reference feature bundle file set drifted — run "
        "STAPEL_REGEN_FLOW_FEATURES=1 pytest tests/test_flow_feature_reference.py "
        "and commit docs/examples/auth-flow-features/"
    )
    drifted = [rel for rel, data in generated.items() if committed.get(rel) != data]
    assert not drifted, (
        f"reference feature bundles are stale: {drifted} — run "
        "STAPEL_REGEN_FLOW_FEATURES=1 pytest tests/test_flow_feature_reference.py "
        "and commit docs/examples/auth-flow-features/"
    )


def test_reference_bundles_cover_three_auth_flows_bilingually():
    for lang in LANGUAGES:
        for flow_id in FLOW_IDS:
            assert (EXAMPLE / lang / f"{flow_id}.feature").is_file()
        assert (EXAMPLE / lang / "steps" / "flows.steps.ts").is_file()
        assert (EXAMPLE / lang / "steps" / "fixtures.ts").is_file()
    # the ru bundle reads in Russian (dialect header + resolved catalog texts)
    ru = (EXAMPLE / "ru" / "auth.passwordless_login.feature").read_text()
    assert ru.startswith("# language: ru\n")
    assert "Дано" in ru and "Когда" in ru
    # the en bundle is the default dialect over the canonical literals
    en = (EXAMPLE / "en" / "auth.passwordless_login.feature").read_text()
    assert "# language:" not in en
    assert "Given The user enters their email on the login form" in en
    # HTTP steps drive the typed client with the snapshot's endpoint bindings
    steps = (EXAMPLE / "en" / "steps" / "flows.steps.ts").read_text()
    assert 'stapel.client.request("/email/request/", { method: "POST" })' in steps
