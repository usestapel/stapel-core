"""stapel_core.flows — business scenarios as first-class objects.

Endpoints, comm actions/tasks and human actions attach to named Flows; the
doc engine (``manage.py generate_flow_docs``) assembles system-analysis
documentation from them, and ``manage.py check_flows`` gates CI on
completeness. One endpoint may participate in several flows.

    # <app>/flows.py — autodiscovered via INSTALLED_APPS
    from stapel_core.flows import Flow

    LOGIN = Flow("auth.passwordless_login", title="...", description="...")

    # on a view method or class:
    @flow_step(LOGIN, order=1, note="Request the code; 429 on rate limit")
    def post(self, request): ...

    # non-HTTP steps:
    LOGIN.action("user.registered", order=3, note="Emitted on first login")
    LOGIN.human(order=0, note="User enters their email")

Texts are i18n-keyed (flow-system.md §2): the literals above are the
canonical (English) source texts, each flow/step derives an implicit key
(``flow.<id>.title`` / ``flow.<id>.step.<order>.note``; explicit
``title_key``/``description_key``/``note_key`` override). Rendering in a
language resolves the keys via per-app ``translations/flows.<lang>.json``
catalogs, the ``translate.resolve`` comm Function and the DOC_TRANSLATOR
seam — see ``stapel_core.flows.i18n``.

See docs: flows-and-verification.md in the stapel workspace.
"""

from .docs import (
    DefaultFlowDocRenderer,
    get_flow_doc_renderer,
    render_flow_markdown,
    render_index_markdown,
)
from .gherkin import (
    FlowSpec,
    gherkin_keywords,
    load_flows_json,
    render_feature,
    render_fixtures,
    render_step_defs,
    write_language_bundle,
)
from .i18n import (
    flow_source_texts,
    load_app_catalogs,
    resolve_flow_texts,
)
from .registry import (
    Flow,
    FlowStep,
    autodiscover_flows,
    flow_registry,
    flow_step,
)

__all__ = [
    "DefaultFlowDocRenderer",
    "Flow",
    "FlowSpec",
    "FlowStep",
    "autodiscover_flows",
    "flow_registry",
    "flow_step",
    "flow_source_texts",
    "get_flow_doc_renderer",
    "gherkin_keywords",
    "load_app_catalogs",
    "load_flows_json",
    "render_feature",
    "render_fixtures",
    "render_flow_markdown",
    "render_index_markdown",
    "render_step_defs",
    "resolve_flow_texts",
    "write_language_bundle",
]
