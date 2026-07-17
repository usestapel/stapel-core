"""Settings namespace of the flows subsystem (``STAPEL_FLOWS``)."""
from stapel_core.conf import AppSettings

flows_settings = AppSettings(
    "STAPEL_FLOWS",
    defaults={
        # DOC_TRANSLATOR seam (attributes-admin-ui.md решение 5 /
        # flow-system.md §2): dotted path to the translator used for flow
        # texts in languages that have no committed catalog and no value in
        # the translate module. The default calls the ``llm.translate`` comm
        # Function *by name* — core stays L0-clean, no import of the agent
        # or translate packages. Protocol:
        #     translate(entries: dict[key, source_text],
        #               source_language: str, target_language: str)
        #         -> dict[key, translated_text]
        "DOC_TRANSLATOR": "stapel_core.i18n.CommDocTranslator",
        # Canonical language of the in-code flow literals (title/description/
        # note strings). Framework convention: English, mirroring the en
        # catalogs committed next to the flows.
        "DOC_SOURCE_LANGUAGE": "en",
        # FLOW_DOC_RENDERER seam (attributes-admin-ui.md решение 5 /
        # flow-system.md §4): dotted path to the renderer that turns a Flow
        # into a markdown SA-document (mermaid step diagram, endpoint tables,
        # step-up verification contracts, index). A module swaps the whole
        # look by pointing this at its own class. Protocol:
        #     render_flow(flow, index, texts, language) -> str
        #     render_index(flows, index, texts, language) -> str
        "FLOW_DOC_RENDERER": "stapel_core.flows.docs.DefaultFlowDocRenderer",
        # Languages the committed doc trees are generated for (flow-system.md
        # §4: en/ru catalogs ship with the module, byte-stable). en is the
        # canonical source; the rest resolve through the i18n chain. Bilingual
        # from day one — `generate_project_docs` writes one tree per language.
        "DOC_LANGUAGES": ["en", "ru"],
    },
    import_strings=("DOC_TRANSLATOR", "FLOW_DOC_RENDERER"),
    no_env=("DOC_TRANSLATOR", "FLOW_DOC_RENDERER"),
)
