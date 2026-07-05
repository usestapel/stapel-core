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
        "DOC_TRANSLATOR": "stapel_core.flows.i18n.CommDocTranslator",
        # Canonical language of the in-code flow literals (title/description/
        # note strings). Framework convention: English, mirroring the en
        # catalogs committed next to the flows.
        "DOC_SOURCE_LANGUAGE": "en",
    },
    import_strings=("DOC_TRANSLATOR",),
    no_env=("DOC_TRANSLATOR",),
)
