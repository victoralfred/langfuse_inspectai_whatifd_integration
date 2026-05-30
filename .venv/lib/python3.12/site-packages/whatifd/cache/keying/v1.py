"""Cache key construction — v1.

Builds a deterministic cache key for a (scorer-configuration, score-case)
pair. The same components MUST produce the same key on every machine,
every Python interpreter, every run. A drift here is a cache-poisoning
footgun.

## What goes into the key

Per `references/contracts.md` ("The `cache_key_components()` method is
critical for determinism"), the key MUST include:

- whatifd report schema version
- whatifd scorer adapter version
- scorer type and package version
- judge provider
- judge model identifier
- judge model snapshot/version (if available)
- rendered judge prompt hash (NOT template hash — the actual final string)
- rubric hash
- scoring parameters
- ScoreCase serialization version
- per-case content hash (so two cases with the same scorer config get
  different keys)

## How the key is constructed

1. Serialize `CacheKeyComponents` to canonical JSON: sorted keys, no
   whitespace, ensure_ascii (no platform-dependent encoding).
2. SHA-256 the canonical bytes.
3. Prefix the hex digest with the version literal:
   `v1:<64-char hex digest>`. The prefix lets storage code split keys
   across versions without ambiguity and makes mismatches grep-able.

## Why a hash, not a structured tuple

The key is used as a filename component (storage layout puts entries at
`.whatifd/cache/entries/<hash[0:2]>/<hash>.json`). A hash gives
fixed-length, filesystem-safe, low-collision keys regardless of how
long the underlying components grow. SHA-256 is overkill for collision
resistance at v0.1 scale; the property we actually need is determinism
across platforms, which SHA-256 provides via stdlib without binary
deps.

## Versioning

`CACHE_KEY_VERSION = "v1"`. PRs that change the component set, the
canonical-JSON shape, or the hashing algorithm MUST introduce a `v2`
module — never mutate `v1`. The version-bump test asserts that any
diff under `whatifd/cache/keying/v1.py` triggers a constant change OR
the diff is rejected (cascade-tracked).

A `v1` key and a `v2` key MUST NOT collide; the version prefix
guarantees this even if the hashes happened to match.

## Canonical encoding lives in `whatifd/serialization/`

The canonical-JSON helper this module uses (`canonical_json_bytes`)
lives in `whatifd/serialization/canonical.py`. Centralizing canonical
encoding there gives:

- A single source of truth for the hash-input canonical form.
- Future-proof scope for the Phase 5 banned-import lint
  (`references/enforcement.md` row 2): all `json.dumps` calls inside
  `whatifd/` already live inside `whatifd/serialization/`, so the lint
  is satisfied without an allowlist.
- A clear semantic boundary: `canonical_json_bytes` is for hash
  inputs (no Sensitive[T] redaction needed); the artifact-path
  encoder (Phase 5) carries the redaction graph walk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from whatifd.exceptions import InvariantViolationError
from whatifd.serialization import canonical_json_bytes

CACHE_KEY_VERSION = "v1"

# Hex-digest validation: any cryptographic hash digest in hex form is
# a non-empty lowercase-hex string. The pattern is intentionally
# algorithm-agnostic (matches SHA-1 40-char, SHA-256 64-char,
# SHA-512 128-char, etc.) — the adapter chooses the algorithm; this
# module enforces only that the value IS a hex digest, not raw text.
#
# Why minimum 16 chars (and not, say, 32 = half-SHA-256)?
# The check exists to catch raw text accidentally bypassing adapter
# hashing, NOT to enforce cryptographic strength (cardinal #5 boundary,
# not a security boundary — adapter responsibility per references/
# contracts.md "cache_key_components"). 16 is the lowest length where
# arbitrary plain text is unlikely to coincidentally pass: short raw
# strings are usually 1-2 words containing spaces / punctuation / non-hex
# letters that fail the [0-9a-f] character class first; legitimate
# digests start at SHA-1's 40 chars and go up. Tightening to 32 would
# reject 24-char xxhash3 and similar non-cryptographic-but-deterministic
# hashes some adapters might choose. If a future cardinal-strength
# tightening is wanted, the cleaner path is per-algorithm validation
# (e.g., HashHex newtype with algorithm-tagged subtypes), not raising
# the floor here.
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{16,}$")


@dataclass(frozen=True, slots=True)
class CacheKeyComponents:
    """All inputs needed for a deterministic cache key.

    Adapter authors construct one of these per (scorer-config,
    score-case) pair via `Scorer.cache_key_components()`. Every field
    contributes to the hash; reordering fields does NOT change the key
    because canonical JSON sorts keys before serialization.

    Hash fields (`rendered_prompt_hash`, `rubric_hash`,
    `scoring_parameters_hash`, `score_case_hash`) are pre-hashed by the
    adapter so this module never sees raw judge prompts or
    user-content. That keeps the key construction free of any
    `Sensitive[T]` exposure (cardinal #5). The pre-hash contract is
    enforced structurally in `__post_init__` — each hash field must
    match `^[0-9a-f]{16,}$` (lowercase hex, ≥16 chars). A raw text
    snippet that accidentally bypassed adapter hashing fails
    construction with `InvariantViolationError` rather than silently
    landing in a cache key.

    `judge_model_snapshot` is `str | None` because not every provider
    exposes a snapshot/version pin; absent providers MUST pass None
    explicitly so the field's presence in the key shape is constant.
    """

    whatif_schema_version: str
    whatif_scorer_adapter_version: str
    scorer_type: str
    scorer_package_version: str
    judge_provider: str
    judge_model_id: str
    judge_model_snapshot: str | None
    rendered_prompt_hash: str
    rubric_hash: str
    scoring_parameters_hash: str
    score_case_serialization_version: str
    score_case_hash: str

    def __post_init__(self) -> None:
        # Cardinal #5 boundary: pre-hashed inputs only. A non-hex value
        # in a hash slot means the adapter forgot to hash, and raw
        # judge prompts / user content would otherwise reach the cache
        # key (where keys are not redacted on disk).
        for field_name in (
            "rendered_prompt_hash",
            "rubric_hash",
            "scoring_parameters_hash",
            "score_case_hash",
        ):
            value = getattr(self, field_name)
            if not _HEX_DIGEST_RE.match(value):
                raise InvariantViolationError(
                    f"CacheKeyComponents.{field_name}={value!r} is not a "
                    "lowercase hex digest of ≥16 characters. The adapter "
                    "is responsible for hashing this field before passing "
                    "it to cache keying — passing raw text here would put "
                    "user content into cache filenames (cardinal #5)."
                )


def build_cache_key(components: CacheKeyComponents) -> str:
    """Return the versioned, content-addressed cache key.

    Output format: `v1:<64-char hex digest>`. The prefix is the active
    `CACHE_KEY_VERSION`; the digest is SHA-256 over canonical JSON of
    the components.

    Determinism: the function is pure. Same components → same key on
    every platform. JSON encoding uses `sort_keys=True`, no whitespace,
    `ensure_ascii=True` so the byte stream is platform-independent.
    """
    digest = hashlib.sha256(canonical_json_bytes(asdict(components))).hexdigest()
    return f"{CACHE_KEY_VERSION}:{digest}"
