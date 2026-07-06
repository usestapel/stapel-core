"""``translate_catalogs`` engine — write-time catalog generation + provenance.

i18n-shipping.md §5. Materialize ``translations/<domain>.<lang>.json`` from a
domain's canonical ``{key: source_en}``, recording provenance in
``translations/.state.json``. Per key, in order:

1. **keep** — the catalog already has a value and the source hash in
   ``.state.json`` matches ``h(source_en)`` → untouched (idempotent, zero diff);
2. **seed** — a curated corpus (``--seed``: the stapel-translate builtin
   fixtures, already paid for) supplies the value → ``origin: seed:<label>``;
3. **llm** — with ``--llm``, the translator seam fills the remainder, through a
   content-hash cache (unchanged sources ⇒ zero LLM calls, zero diff) →
   ``origin: llm`` (machine, unreviewed — the gate's W-counter);
4. **leave unset** — otherwise the key stays missing and the gate fails loudly.

``--approve`` flips reviewed keys to ``origin: human`` without retranslating —
review is a state transition, not hand-editing JSON.

Pure over its inputs (a directory + ``source_texts``) so it is unit-testable
without a management-command harness or a real LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .catalogs import (
    ORIGIN_HUMAN,
    ORIGIN_LLM,
    STATE_FILENAME,
    DocTranslationCache,
    StateSidecar,
    catalog_filename,
    content_hash,
    dump_catalog,
    is_reviewed,
    load_catalog_file,
)


@dataclass
class TranslateResult:
    kept: int = 0
    seeded: int = 0
    translated: int = 0
    approved: int = 0
    missing: list[str] = field(default_factory=list)
    written: bool = False
    catalog_path: Path | None = None

    @property
    def unreviewed(self) -> int:  # machine-translated this run
        return self.translated


def translate_catalog(
    domain: str,
    language: str,
    out_dir: Path | str,
    *,
    source_texts: dict[str, str],
    source_language: str = "en",
    seed: dict[str, str] | None = None,
    seed_label: str = "seed",
    llm: bool = False,
    translator=None,
    approve: list[str] | None = None,
    approve_all: bool = False,
) -> TranslateResult:
    """Generate/refresh one ``<domain>.<lang>.json`` catalog under *out_dir*.

    *out_dir* is the ``translations`` directory. *seed* is a flat
    ``{key: text}`` corpus (keys outside *source_texts* are ignored). *approve*
    is a list of keys to mark reviewed (``origin: human``); *approve_all* marks
    every present key reviewed. Approval never retranslates.
    """
    out = Path(out_dir)
    catalog = load_catalog_file(out / catalog_filename(domain, language))
    state = StateSidecar(out / STATE_FILENAME)
    seed = seed or {}
    result = TranslateResult(catalog_path=out / catalog_filename(domain, language))

    # --- approval pass: a pure state transition over already-present values ---
    # Approving blesses the value against the *current* source (clears any
    # staleness) — the reviewer looked at what ships now.
    if approve_all or approve:
        wanted = set(source_texts) if approve_all else set(approve or [])
        for key in wanted:
            if key not in catalog:
                continue
            state.set(domain, language, key,
                      source_hash=content_hash(source_texts.get(key, "")),
                      origin=ORIGIN_HUMAN)
            result.approved += 1

    # --- translation pass -------------------------------------------------
    to_llm: dict[str, str] = {}
    for key, source_en in source_texts.items():
        src_hash = content_hash(source_en)
        st = state.get(domain, language, key)
        origin = (st or {}).get("origin")
        # 1. keep: fresh value with matching source hash (reviewed or not).
        if key in catalog and st is not None and st.get("hash") == src_hash:
            result.kept += 1
            continue
        # Value present with stale state (the en source changed after this
        # translation): a reviewed value stays put and is left STALE for the
        # gate to flag (never silently re-blessed) unless --llm retranslates it.
        if key in catalog and st is not None:
            if is_reviewed(origin) and not llm:
                continue
            # machine value, or --llm on a stale reviewed value → retranslate
            # (seed first if the corpus has it).
            if key in seed and seed[key]:
                catalog[key] = seed[key]
                state.set(domain, language, key,
                          source_hash=src_hash, origin=f"seed:{seed_label}")
                result.seeded += 1
                continue
            if llm:
                to_llm[key] = source_en
            continue
        # Value present, NO sidecar (a hand-written catalog being onboarded):
        # record its provenance against the current source (fresh by assumption).
        if key in catalog and st is None:
            state.set(domain, language, key, source_hash=src_hash, origin=ORIGIN_HUMAN)
            result.kept += 1
            continue
        # 2. seed a missing key from the curated corpus.
        if key in seed and seed[key]:
            catalog[key] = seed[key]
            state.set(domain, language, key,
                      source_hash=src_hash, origin=f"seed:{seed_label}")
            result.seeded += 1
            continue
        # 3. queue a still-missing key for the LLM seam (opt-in).
        if llm:
            to_llm[key] = source_en

    if to_llm and llm:
        cache = DocTranslationCache(out / f".{domain}.{language}.llm-cache.json")
        pending: dict[str, str] = {}
        for key, source_en in to_llm.items():
            cached = cache.get(key, source_en)
            if cached is not None:
                catalog[key] = cached
                state.set(domain, language, key,
                          source_hash=content_hash(source_en), origin=ORIGIN_LLM)
                result.translated += 1
            else:
                pending[key] = source_en
        if pending:
            if translator is None:
                from .conf import i18n_settings

                translator = i18n_settings.TRANSLATOR()
            out_texts = translator.translate(pending, source_language, language) or {}
            for key, text in out_texts.items():
                if key in pending and isinstance(text, str) and text:
                    catalog[key] = text
                    cache.put(key, pending[key], text)
                    state.set(domain, language, key,
                              source_hash=content_hash(pending[key]), origin=ORIGIN_LLM)
                    result.translated += 1
        cache.save()

    result.missing = sorted(k for k in source_texts if k not in catalog)

    # Drop provenance / catalog keys the canon no longer has? Keep catalog
    # orphans (host overrides) but prune stale sidecar rows for gone keys.
    state.prune(domain, language, catalog.keys())

    # --- write (byte-stable) ---------------------------------------------
    catalog_path = out / catalog_filename(domain, language)
    new_bytes = dump_catalog(catalog)
    old_bytes = catalog_path.read_text(encoding="utf-8") if catalog_path.is_file() else None
    if new_bytes != old_bytes:
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(new_bytes, encoding="utf-8")
        result.written = True
    state.save()
    return result


__all__ = ["TranslateResult", "translate_catalog"]
