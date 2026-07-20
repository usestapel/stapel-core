"""stapel_core.media — source-agnostic image descriptor (builder + DRF)."""
import pytest

pytest.importorskip("drf_spectacular")

from stapel_core.media import from_render_metadata, image  # noqa: E402
from stapel_core.media.drf import (  # noqa: E402
    RenderMetadataSerializer,
    StapelImageSerializer,
    VariantMetaSerializer,
    describe_or_none,
)

_RM = {
    "mime": "image/webp",
    "bytes": 12345,
    "width": 1200,
    "height": 800,
    "aspect": 1.5,
    "duration_ms": None,
    "preview_b64": "data:image/webp;base64,AAAA",
    "square": False,
    "variants": [
        {"tier": 320, "branch": "w", "url": "/m/a_320w.webp", "width": 320, "height": 213},
        {"tier": 64, "branch": None, "url": "/m/a_64.webp", "width": 64, "height": 64},
        {"tier": "original", "branch": None, "url": "/m/a.webp", "width": 1200, "height": 800},
    ],
}


class TestRenderMetadataSerializer:
    def test_round_trips_a_snapshot(self):
        data = RenderMetadataSerializer(_RM).data
        assert data["mime"] == "image/webp" and data["aspect"] == 1.5
        assert len(data["variants"]) == 3

    def test_tier_stringified_on_the_wire(self):
        # int tiers become decimal strings; "original" stays literal — one
        # scalar type, matching the DTO-declared schema.
        tiers = [v["tier"] for v in RenderMetadataSerializer(_RM).data["variants"]]
        assert "320" in tiers and "original" in tiers
        assert all(isinstance(t, str) for t in tiers)

    def test_variant_branch_nullable(self):
        v = VariantMetaSerializer({"tier": 64, "branch": None, "url": "/x", "width": 64, "height": 64}).data
        assert v["branch"] is None


class TestImageBuilder:
    def test_link_passthrough_no_ladder(self):
        img = image("link", "https://cdn.example/oauth-avatar.png", aspect=1.0)
        assert img["source"] == "link"
        assert img["url"] == "https://cdn.example/oauth-avatar.png"
        assert img["variants"] == []
        assert img["aspect"] == 1.0 and img["preview_b64"] is None

    def test_empty_value_is_none(self):
        assert image("cdn", None) is None
        assert image("cdn", "") is None
        assert image("link", "") is None

    def test_cdn_routes_to_cdn_provider_regardless_of_backend(self, monkeypatch):
        # libgaps Н3: a cdn-sourced ref must be described by the CDN provider
        # (its own naming), NOT the global backend (which may be pil and would
        # find zero variants). Patch the CDN provider's describe.
        import stapel_core.media.descriptor as mod

        monkeypatch.setattr(
            mod.CdnRenderMetadataProvider, "describe", lambda self, ref: _RM
        )
        img = image("cdn", "avatar/live")
        assert img["source"] == "cdn"
        # top-level url lifts the "original" variant
        assert img["url"] == "/m/a.webp"
        assert img["aspect"] == 1.5
        assert len(img["variants"]) == 3

    def test_file_routes_to_pil_provider(self, monkeypatch):
        import stapel_core.media.descriptor as mod

        monkeypatch.setattr(
            mod.PilRenderMetadataProvider, "describe", lambda self, ref: _RM
        )
        img = image("file", "avatars/ada.png")
        assert img["source"] == "file" and len(img["variants"]) == 3

    def test_unresolvable_ref_degrades_to_none(self, monkeypatch):
        import stapel_core.media.descriptor as mod

        def _raise(self, ref):
            raise LookupError(ref)

        monkeypatch.setattr(mod.CdnRenderMetadataProvider, "describe", _raise)
        assert image("cdn", "avatar/gone") is None

    def test_from_render_metadata_picks_original_url(self):
        img = from_render_metadata("file", _RM)
        assert img["source"] == "file" and img["url"] == "/m/a.webp"

    def test_from_render_metadata_falls_back_to_largest_when_no_original(self):
        rm = {**_RM, "variants": [
            {"tier": 64, "branch": None, "url": "/m/a_64.webp", "width": 64, "height": 64},
            {"tier": 320, "branch": "w", "url": "/m/a_320w.webp", "width": 320, "height": 213},
        ]}
        img = from_render_metadata("file", rm)
        assert img["url"] == "/m/a_320w.webp"  # largest area


class TestStapelImageSerializer:
    def test_serializes_cdn_ladder(self):
        img = from_render_metadata("cdn", _RM)
        data = StapelImageSerializer(img).data
        assert data["source"] == "cdn" and data["url"] == "/m/a.webp"
        assert len(data["variants"]) == 3

    def test_serializes_link_without_ladder(self):
        img = image("link", "https://x/y.png")
        data = StapelImageSerializer(img).data
        assert data["source"] == "link" and data["variants"] == []
        assert data["preview_b64"] is None


class TestDescribeOrNone:
    def test_empty_and_unresolvable(self, monkeypatch):
        import stapel_core.media.drf as drf

        assert describe_or_none(None) is None
        monkeypatch.setattr(drf, "describe", lambda ref: (_ for _ in ()).throw(LookupError(ref)))
        assert describe_or_none("avatar/x") is None
