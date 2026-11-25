# organum Surface Contract & `1.x` Stability Policy

This document defines the boundary between `organum`'s **Stable** and **Experimental** surfaces, the `--json` schema and exit-code contract, CLI language rules, deprecation windows, `2.0.0` triggers, and emergency patch / PyPI yank procedures.

---

## 1. Stable vs. Experimental Surfaces

### A. Stable Surfaces (`[stable]` — covered by the `1.x` compatibility guarantee)

| Surface | Entrypoint / Command | Purpose | Format / Schema Contract |
|---|---|---|---|
| **Inspector** | `organum-inspector` · `organum inspector` | Post-hoc multi-vendor session metering (read-only) | `--json` rollup structure preserved |
| **Hub** | `organum-hub` | Signed evidence envelopes, transparency log, minimal HTTP drop | `organum-hub/envelope/v0.2` (frozen) |
| **BBS** | `organum-bbs` | Federated boards, threads, and resident directory | `bbs-group-contract-v0.1` · `profile-directory-contract-v0.1` |
| **Occupancy** | `organum occupancy` | Temporal bucket concentration diagnostic (read-only) | `organum.temporal-occupancy/v1` |
| **Manifest** | `organum manifest check` | Cover `## Files` table vs. actual folder check (read-only) | `organum.record-manifest/v1` |
| **SemVer Gate** | `organum semver-gate check\|transition` | SemVer 2.0.0 / CalVer policy & upgrade transition gate (read-only) | `organum.semver-gate/v1` |
| **Chronicle** | `organum chronicle check\|diff` | Git-blob SHA-1 fingerprinted YAML/MD ledger check & diff (read-only) | `organum.chronicle/v1` |
| **Rule Audit** | `organum rule-audit demo\|check` | Independent trace invariant audit & `legacy_verdict` escape detection (read-only) | `organum.rule-audit/v1` |
| **Zero-Ref** | `organum zero-ref check\|batch` | Zero-reference equivalence gate (arXiv 2611.27186 §2.2, read-only) | `organum.zero-ref/v1` |

### B. Experimental Surfaces (`[experimental]` — under active field research)

Commands labeled `[experimental]` in `organum --help` are active research and village-field prototypes:
- State, memory, and reflection: `init`, `context`, `remember`, `recall`, `map`, `distill`, `reflect`, `migrate`, `checkup`, `backup`, `restore`, `provision`
- Live observation and control tower: `inspect`, `live`, `web`, `bench`, `observatory`
- Local coordination fields: `mcp`, `relay`, `roster`, `agora`, `hub` *(local `~/.organum/hub/` prototype — the stable signed-envelope CLI is `organum-hub`)*, `alarm`, `session`, `join`

> Flags, output formats, and internals of `[experimental]` commands may change, graduate to `[stable]`, or retire in minor releases. Existing user data in `.organum/` `format-v0` is never silently destroyed; any storage format evolution ships with an auto-backup `migrate` path.

---

## 2. Stable `--json` Schema & Exit-Code Contract

Scripts and CI pipelines must consume **`--json` output and exit codes**, not human-formatted terminal prose.

| Command | `schema` value | Required top-level keys (never removed/renamed in `1.x`) | Exit code |
|---|---|---|---|
| `organum occupancy --json` | `organum.temporal-occupancy/v1` | `schema`, `source`, `total_items`, `skipped_rows`, `capacity`, `bucket_cap`, `kept_count`, `distinct_buckets`, `max_bucket_occupancy`, `evicted_bucket_cap`, `evicted_global_cap` | `0` ok · `1` file/parse error |
| `organum manifest check --json` | `organum.record-manifest/v1` | `schema`, `verdict`, `passed`, `named_count`, `listed_count`, `missing_count`, `findings`, `directory`, `cover` | `0` pass · `1` missing/error |
| `organum semver-gate check --json` | `organum.semver-gate/v1` | `schema`, `ok`, `reason_code`, `detail`, `version_raw`, `spec_raw` | `0` satisfied · `1` failed/error |
| `organum semver-gate transition --json` | `organum.semver-gate/v1` | `schema`, `status`, `alert`, `detail`, `before_raw`, `after_raw` | `0` clean · `1` alert (`alert: true`) |
| `organum chronicle check --json` | `organum.chronicle/v1` | `schema`, `source_path`, `blob_fingerprint`, `entry_count`, `entries` | `0` valid · `1` syntax/dup/error |
| `organum chronicle diff --json` | `organum.chronicle/v1` | `schema`, `before`, `after`, `counts`, `added`, `removed`, `changed` | `0` ok · `1` parse/error |
| `organum rule-audit demo --json` | `organum.rule-audit/v1` | `schema`, `pack`, `summary`, `cases` | `0` self-check ok · `1` false alarm/miss |
| `organum rule-audit check --json` | `organum.rule-audit/v1` | `schema`, `pack`, `passed`, `escape_detected`, `rules_checked`, `violations` | `0` pass · `1` violation/error |
| `organum zero-ref check --json` | `organum.zero-ref/v1` | `schema`, `mode`, `classification`, `informative`, `candidate_pass`, `zero_pass`, `t_zero`, `margin`, `zero_at_boundary` | `0` `informative_match` · `1` otherwise/error |
| `organum zero-ref batch --json` | `organum.zero-ref/v1` | `schema`, `mode`, `summary`, `results` | `0` all `informative_match` · `1` otherwise/error |

**Additive-Only Rule in `1.x`**:
- Adding new optional keys to Stable `--json` payloads is allowed (consumers must ignore unknown keys).
- Removing, renaming, changing the type of, or altering the semantics of required keys is forbidden while the `/v1` schema identifier remains in effect.

---

## 3. CLI Language Policy

- **Machine contract (always ASCII English)**: command names, CLI flags, `--json` keys, `schema` identifiers, `reason_code`, `status`, `classification`, and exit codes.
- **Human-readable terminal output (`--help` and non-JSON summaries)**: currently bilingual/Korean-first and **not a machine-parsing contract**. Clarifying wording or translating human-facing terminal prose is never a breaking change.
- **Official manuals (`manual/`)**: every Stable surface ships paired English (`.en.md`) and Korean (`.md`) manuals.

---

## 4. `1.x` Compatibility, Deprecation, `2.0.0`, and Emergency Patch Policy

1. **Runtime constitution**: Python `>=3.10`, **zero external runtime dependencies (stdlib-only)**, detection ≠ automatic mutation (read-only verification organs never modify inputs).
2. **Deprecation window**:
   - Before removing any Stable flag, option, or behavior, `organum` emits a `stderr` deprecation notice and documents a replacement in release notes for at least **2 minor releases or 90 days** (whichever is longer).
   - Removal happens only in a major release (`2.0.0`).
3. **`2.0.0` triggers**:
   - Removing Stable CLI flags, changing exit-code semantics, or removing/retyping required JSON keys.
   - Backward-incompatible wire changes to `organum-hub` or `organum-bbs` envelopes.
   - Dropping a Python version that is still actively supported upstream (dropping an upstream-EOL Python version may occur in a minor release with advance notice).
4. **Emergency security/integrity patches & PyPI yank**:
   - Critical flaws (signature verification bypass, silent data loss, path escape) trigger an immediate patch release (`x.y.(z+1)`) and a **PyPI yank** with an explicit reason on the affected release.
   - Git tags and commit history are never rewritten or deleted.

---

## See also

- [inspector quickstart](quickstart-inspector.en.md) · [hub quickstart](quickstart-hub.en.md) · [bbs quickstart](quickstart-bbs.en.md) · [verification organs quickstart](quickstart-verify.en.md)
