"""stapel_core.media — one interface, two storage paths (images-and-cdn.md §1).

Covers the pure ladder math (scaled_size / plan_variants / variant_name),
the PIL engine (generate_variants over a FileSystemStorage), the PIL
describe() snapshot (§5 form), the cdn delegation, and the BACKEND swap.
"""
import base64
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import override_settings

from stapel_core.media import (
    CdnRenderMetadataProvider,
    PilRenderMetadataProvider,
    RenderMetadataProvider,
    describe,
    generate_variants,
    get_provider,
    is_square,
    plan_variants,
    scaled_size,
    variant_name,
)

PIL = pytest.importorskip("PIL")
from PIL import Image as PILImage  # noqa: E402


# ---------------------------------------------------------------------------
# Pure plan math
# ---------------------------------------------------------------------------

class TestScaledSize:
    def test_axis_w(self):
        assert scaled_size(1000, 500, 250, "w") == (250, 125)

    def test_axis_h(self):
        assert scaled_size(1000, 500, 250, "h") == (500, 250)

    def test_axis_min(self):
        # min side is height
        assert scaled_size(1000, 500, 250, "min") == (500, 250)

    def test_no_upscale(self):
        for axis in ("w", "h", "min"):
            assert scaled_size(100, 50, 500, axis) == (100, 50)

    def test_unknown_axis(self):
        with pytest.raises(ValueError):
            scaled_size(10, 10, 5, "diag")


class TestPlan:
    def test_portrait_full_plan(self):
        plan = plan_variants(600, 1200, thumbnail_sizes=[16, 32, 64, 120],
                             preview_sizes=[160, 240, 480, 560, 720, 1080])
        entries = {(p.tier, p.branch): p for p in plan}
        # 4 min-side thumbnails + 6 tiers × 2 branches
        assert len(entries) == 4 + 12
        assert entries[(120, None)].width == 120  # min side = width (portrait)
        assert entries[(560, "w")].width == 560
        assert entries[(560, "h")].height == 560
        # no-upscale cap: native width 600 < 720/1080
        assert entries[(1080, "w")].width == 600

    def test_square_gets_single_branch(self):
        plan = plan_variants(500, 501, thumbnail_sizes=[16],
                             preview_sizes=[160, 480])
        branches = {p.branch for p in plan if p.branch is not None}
        assert branches == {"w"}  # 1px epsilon → square (§3.3)

    def test_is_square_epsilon(self):
        assert is_square(500, 500)
        assert is_square(500, 501)
        assert not is_square(500, 502)


class TestVariantName:
    def test_sibling_naming(self):
        assert variant_name("photos/cat.jpg", 560, "w") == "photos/cat__560w.webp"
        assert variant_name("photos/cat.jpg", 120, None) == "photos/cat__120.webp"

    def test_name_without_extension(self):
        assert variant_name("blob", 16, None) == "blob__16.webp"


# ---------------------------------------------------------------------------
# PIL engine + provider
# ---------------------------------------------------------------------------

@pytest.fixture
def storage(tmp_path):
    return FileSystemStorage(location=str(tmp_path), base_url="/media/")


@pytest.fixture
def stored_portrait(storage):
    buf = BytesIO()
    PILImage.new("RGB", (600, 1200), color="blue").save(buf, format="JPEG")
    name = storage.save("photos/portrait.jpg", ContentFile(buf.getvalue()))
    return name


class _FieldFileStandIn:
    """Minimal FieldFile-alike: .name, .storage, .open()."""

    def __init__(self, storage, name):
        self.storage = storage
        self.name = name

    def open(self, mode="rb"):
        return self.storage.open(self.name, mode)


class TestGenerateVariants:
    def test_generates_ladder_files(self, storage, stored_portrait):
        entries = generate_variants(_FieldFileStandIn(storage, stored_portrait))

        assert len(entries) == 4 + 12  # portrait: both branches
        by_key = {(e["tier"], e["branch"]): e for e in entries}
        assert storage.exists("photos/portrait__120.webp")
        assert storage.exists("photos/portrait__560w.webp")
        assert storage.exists("photos/portrait__560h.webp")

        # Geometry recorded truthfully (incl. the no-upscale cap).
        assert by_key[(560, "w")]["width"] == 560
        assert by_key[(1080, "w")]["width"] == 600
        assert by_key[(120, None)]["width"] == 120  # min side

        # The files really have the planned geometry.
        with storage.open("photos/portrait__560h.webp") as fh:
            img = PILImage.open(fh)
            assert img.size == (280, 560)

    def test_square_original_single_branch(self, storage):
        buf = BytesIO()
        PILImage.new("RGB", (400, 400), color="red").save(buf, format="JPEG")
        name = storage.save("sq.jpg", ContentFile(buf.getvalue()))

        entries = generate_variants(_FieldFileStandIn(storage, name))

        assert not storage.exists("sq__480h.webp")
        assert storage.exists("sq__480w.webp")
        branches = {e["branch"] for e in entries if e["branch"] is not None}
        assert branches == {"w"}

    def test_watermark_applied_to_previews_only(self, storage, stored_portrait):
        seen_sizes = []

        def marker(img):
            seen_sizes.append(img.size)
            return img

        generate_variants(_FieldFileStandIn(storage, stored_portrait), watermark=marker)
        # called once per preview branch file (12), never for thumbnails
        assert len(seen_sizes) == 12
        assert all(max(size) > 120 for size in seen_sizes)


class TestPilDescribe:
    def test_snapshot_shape(self, storage, stored_portrait):
        generate_variants(_FieldFileStandIn(storage, stored_portrait))
        snapshot = PilRenderMetadataProvider(storage=storage).describe(stored_portrait)

        assert snapshot["mime"] == "image/jpeg"
        assert snapshot["width"] == 600 and snapshot["height"] == 1200
        assert snapshot["aspect"] == 0.5
        assert snapshot["duration_ms"] is None
        assert snapshot["square"] is False
        assert snapshot["preview_b64"].startswith("data:image/webp;base64,")
        # the inlined bytes ARE the 16px tier file
        with storage.open(variant_name(stored_portrait, 16, None), "rb") as fh:
            assert snapshot["preview_b64"].split(",", 1)[1] == base64.b64encode(
                fh.read()
            ).decode("ascii")

        keys = {(v["tier"], v["branch"]) for v in snapshot["variants"]}
        assert (16, None) in keys
        assert (560, "w") in keys and (560, "h") in keys
        assert ("original", None) in keys
        urls = [v["url"] for v in snapshot["variants"]]
        assert all(u.startswith("/media/") for u in urls)

    def test_variants_absent_from_snapshot_until_generated(self, storage, stored_portrait):
        snapshot = PilRenderMetadataProvider(storage=storage).describe(stored_portrait)
        # only the original — no generated files yet, no preview_b64
        assert [v["tier"] for v in snapshot["variants"]] == ["original"]
        assert snapshot["preview_b64"] is None

    def test_unknown_ref_raises(self, storage):
        with pytest.raises(LookupError):
            PilRenderMetadataProvider(storage=storage).describe("nope.jpg")


# ---------------------------------------------------------------------------
# Backend swap (§1: config, not a code branch)
# ---------------------------------------------------------------------------

class TestBackendSwap:
    def test_default_is_pil(self):
        assert isinstance(get_provider(), PilRenderMetadataProvider)

    def test_cdn_backend_via_namespace(self):
        with override_settings(STAPEL_MEDIA={"BACKEND": "cdn"}):
            assert isinstance(get_provider(), CdnRenderMetadataProvider)

    def test_flat_stapel_media_backend_setting(self):
        with override_settings(STAPEL_MEDIA_BACKEND="cdn"):
            assert isinstance(get_provider(), CdnRenderMetadataProvider)

    def test_dotted_path_escape_hatch(self):
        with override_settings(
            STAPEL_MEDIA={"BACKEND": "stapel_core.media.PilRenderMetadataProvider"}
        ):
            assert isinstance(get_provider(), PilRenderMetadataProvider)

    def test_providers_satisfy_protocol(self):
        assert isinstance(PilRenderMetadataProvider(), RenderMetadataProvider)
        assert isinstance(CdnRenderMetadataProvider(), RenderMetadataProvider)

    def test_cdn_provider_delegates_to_comm(self, monkeypatch):
        calls = []

        def fake_call(name, payload):
            calls.append((name, payload))
            return {"mime": "image/webp", "variants": []}

        import stapel_core.comm as comm

        monkeypatch.setattr(comm, "call", fake_call)
        with override_settings(STAPEL_MEDIA={"BACKEND": "cdn"}):
            result = describe("product/" + "a" * 64)
        assert calls == [("cdn.describe", {"ref": "product/" + "a" * 64})]
        assert result["mime"] == "image/webp"
