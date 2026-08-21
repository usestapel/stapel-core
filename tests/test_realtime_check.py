"""Tests for the realtime border gate (stapel_core.lint.realtime_check)."""
import textwrap
from pathlib import Path

from stapel_core.lint.realtime_check import (
    GRANDFATHERED,
    SANCTIONED,
    SANCTIONED_PROJECTS,
    allowance,
    check_source,
    iter_python_files,
    main,
    project_name,
)


def _check(src: str, path: str = "mod.py"):
    return check_source(textwrap.dedent(src), Path(path))


def _codes(src: str):
    return [f.code for f in _check(src)]


# ---------------------------------------------------------------------------
# RT001 — a fifth hand-rolled Channels consumer
# ---------------------------------------------------------------------------


def test_consumer_import_flagged():
    assert _codes("""
        from channels.generic.websocket import AsyncWebsocketConsumer
    """) == ["RT001"]


def test_consumer_subclass_flagged():
    codes = _codes("""
        class LobbyConsumer(Base):
            pass

        class DialogConsumer(AsyncJsonWebsocketConsumer):
            pass
    """)
    assert codes == ["RT001"]


def test_dotted_consumer_base_flagged():
    assert _codes("""
        class C(websocket.AsyncWebsocketConsumer):
            pass
    """) == ["RT001"]


def test_plain_class_not_flagged():
    assert _codes("""
        class RecordingService:
            def save(self): ...
    """) == []


def test_channels_routing_import_is_not_the_border():
    # Assembling an ASGI app from routing manifests is a host's job; the
    # border is about consumers and socket auth, not URLRouter.
    assert _codes("""
        from channels.routing import ProtocolTypeRouter, URLRouter
    """) == []


# ---------------------------------------------------------------------------
# RT002 — socket auth has exactly one home (core G14)
# ---------------------------------------------------------------------------


def test_channels_auth_import_flagged():
    assert _codes("from channels.auth import AuthMiddlewareStack") == ["RT002"]


def test_channels_middleware_import_flagged():
    assert _codes("from channels.middleware import BaseMiddleware") == ["RT002"]


def test_asgi_middleware_shape_flagged():
    assert _codes("""
        class JWTAuthMiddleware:
            def __init__(self, inner):
                self.inner = inner

            async def __call__(self, scope, receive, send):
                return await self.inner(scope, receive, send)
    """) == ["RT002"]


def test_other_callables_not_mistaken_for_middleware():
    assert _codes("""
        class Renderer:
            def __call__(self, value):
                return str(value)
    """) == []


# ---------------------------------------------------------------------------
# RT003 — a raw websockets server outside the machine-protocol home
# ---------------------------------------------------------------------------


def test_websockets_serve_flagged():
    assert _codes("""
        import websockets

        async def run():
            async with websockets.serve(handler, host, port):
                await forever()
    """) == ["RT003"]


def test_websockets_client_is_not_a_new_implementation():
    # A smoke script that connects to an existing socket invents nothing.
    assert _codes("""
        import websockets

        async def smoke():
            async with websockets.connect(url) as ws:
                await ws.send("ping")
    """) == []


def test_unrelated_serve_call_not_flagged():
    assert _codes("""
        def run():
            grpc_server.serve()
    """) == []


# ---------------------------------------------------------------------------
# RT004 / RT005 — warnings, not build breakers
# ---------------------------------------------------------------------------


def test_sse_endpoint_warned():
    findings = _check("""
        def stream(request):
            return StreamingHttpResponse(tee(), content_type="text/event-stream")
    """)
    assert [(f.code, f.severity) for f in findings] == [("RT004", "warning")]


def test_event_stream_string_alone_not_flagged():
    # Reading an upstream's content-type is not publishing an SSE endpoint.
    assert _codes("""
        def is_stream(resp):
            return "text/event-stream" in resp.headers["content-type"]
    """) == []


def test_channel_layer_fanout_warned():
    findings = _check("""
        def notify(group, message):
            layer = get_channel_layer()
            async_to_sync(layer.group_send)(group, message)
    """)
    # group_send here is passed by reference to async_to_sync, so the layer
    # lookup is the hit — one finding per file is the point, not a count.
    assert [(f.code, f.severity) for f in findings] == [("RT005", "warning")]


def test_awaited_group_send_warned():
    findings = _check("""
        async def notify(layer, group, message):
            await layer.group_send(group, message)
    """)
    assert [f.code for f in findings] == ["RT005"]


def test_warnings_do_not_fail_the_run(tmp_path, capsys):
    (tmp_path / "realtime.py").write_text(
        "def notify(g, m):\n    layer = get_channel_layer()\n"
    )
    assert main([str(tmp_path)]) == 0
    assert "RT005" in capsys.readouterr().out


def test_errors_fail_the_run(tmp_path, capsys):
    (tmp_path / "consumers.py").write_text(
        "from channels.generic.websocket import AsyncWebsocketConsumer\n"
    )
    assert main([str(tmp_path)]) == 1
    assert "RT001" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Suppression and file selection
# ---------------------------------------------------------------------------


def test_pragma_suppresses():
    assert _codes("""
        from channels.generic.websocket import AsyncWebsocketConsumer  # realtime-check: ok — vendored shim
    """) == []


def test_tests_and_migrations_are_skipped(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helper.py").write_text("import websockets\n")
    (tmp_path / "test_thing.py").write_text("import websockets\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    found = {p.name for p in iter_python_files([tmp_path])}
    assert found == {"app.py"}


# ---------------------------------------------------------------------------
# The allowlists — the debt register, and what it protects
# ---------------------------------------------------------------------------


def test_core_g14_middleware_is_sanctioned():
    # The core's own socket auth is the home the rule points everyone at; it
    # must not trip its own gate.
    path = Path(__file__).resolve().parent.parent / "django" / "jwt" / "channels.py"
    assert project_name(path) == "stapel-core"
    assert allowance(path, project_name(path)) is not None


def test_core_repo_is_clean_under_its_own_gate(capsys):
    repo = Path(__file__).resolve().parent.parent
    assert main([str(repo)]) == 0


def test_allowlist_is_project_qualified():
    # A bare "consumers.py" suffix in another project must NOT inherit
    # stapel-chat's grandfathering — the flat fleet layout makes that the
    # whole point of qualifying by distribution name.
    assert allowance(Path("consumers.py"), "stapel-chat") is not None
    assert allowance(Path("consumers.py"), "stapel-listings") is None
    assert allowance(Path("consumers.py"), None) is None


def test_sanctioned_projects_need_no_entries():
    assert allowance(Path("transport_ws.py"), "stapel-runner-protocol") is not None
    assert allowance(Path("consumer.py"), "stapel-realtime") is not None


def test_every_grandfathered_entry_names_its_migration_phase():
    # The list is a debt register, not a mute button: an entry without the
    # phase that deletes it is a permanent exemption in disguise.
    for _project, suffix, reason in GRANDFATHERED:
        assert "phase" in reason, suffix


def test_sanctioned_entries_are_specific():
    for _project, suffix, _reason in SANCTIONED + GRANDFATHERED:
        assert suffix.endswith(".py")
    assert set(SANCTIONED_PROJECTS) == {"stapel-realtime", "stapel-runner-protocol"}


def test_unqualified_entries_are_multi_segment():
    # An entry with no project name matches on path alone (studio has no
    # pyproject.toml), so it has to carry enough path to be unambiguous.
    for project, suffix, _reason in SANCTIONED + GRANDFATHERED:
        if project is None:
            assert "/" in suffix, suffix


def test_project_name_reads_the_nearest_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "stapel-thing"\n')
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x = 1\n")
    assert project_name(pkg / "mod.py") == "stapel-thing"


def test_project_name_is_none_without_a_pyproject(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert project_name(tmp_path / "mod.py") is None


def test_grandfathered_file_is_reported_but_passes(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "stapel-chat"\n')
    (tmp_path / "consumers.py").write_text(
        "from channels.generic.websocket import AsyncWebsocketConsumer\n"
    )
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "allowed" in out and "phase 2" in out
