# organum verification & hygiene organs quickstart (0.11.0)

`organum 0.11.0` adds six **read-only, stdlib-only** verification and hygiene organs built to catch six quiet failures that agent teams hit repeatedly in operations and research: temporal memory monoculture, missing files behind cover tables, misleading CLI version transitions, silent ledger corruption, verifier escapes, and vacuous equivalence passes.

Three shared rules:
1. **Detection ≠ mutation**: none of the six commands modifies input files, ledgers, or repositories.
2. **Zero external dependencies**: deterministic and offline on Python `>=3.10` standard library alone (`pip install organum`).
3. **Machine-readable contract (`--json`)**: every command emits a versioned `organum.<name>/v1` JSON schema with explicit `0`/`1` exit codes.

| Command | What it checks | `--json` schema | Exit code |
|---|---|---|---|
| `organum occupancy` | Temporal bucket concentration (burst monoculture) in memory JSONL | `organum.temporal-occupancy/v1` | `0` ok · `1` file/parse error |
| `organum manifest check` | Cover `## Files` table vs. actual directory & receipt files | `organum.record-manifest/v1` | `0` pass · `1` missing/error |
| `organum semver-gate` | SemVer 2.0.0 / CalVer policy ranges & CLI upgrade transitions | `organum.semver-gate/v1` | `0` ok · `1` fail/alert |
| `organum chronicle` | Git-blob SHA-1 fingerprinted flat YAML / dated MD ledger check & diff | `organum.chronicle/v1` | `0` ok · `1` syntax/dup/error |
| `organum rule-audit` | Independent trace invariant audit + `legacy_verdict` escape detection | `organum.rule-audit/v1` | `0` pass · `1` violation/error |
| `organum zero-ref` | Zero-reference equivalence gate (arXiv 2611.27186 §2.2, exact `Decimal`) | `organum.zero-ref/v1` | `0` informative · `1` vacuous/mismatch/error |

---

## 1. `organum occupancy` — temporal bucket occupancy diagnostic

Measures whether a burst of recent entries is crowding out older context in a memory ledger (**burst monoculture**), without touching the ledger.

```bash
# Default: inspect .organum/memory/memories.jsonl (24 slots, 24h buckets, cap 6/bucket)
organum occupancy

# Inspect any JSONL ledger with 1-hour buckets and cap 4, JSON output
organum occupancy --file path/to/facts.jsonl --capacity 32 --bucket-hours 1 --bucket-cap 4 --json
```

- Automatically probes `ts`, `created_at`, `at`, and `timestamp` fields; unparseable rows are counted honestly in `skipped_rows` rather than silently dropped.
- **Honest boundary**: diagnostic only. It never compacts, deletes, or enforces retention on your ledger.

---

## 2. `organum manifest check` — cover (`## Files`) manifest verifier

Checks that every file named in a record folder's cover table (`README.md` `## Files`) actually exists on disk.

```bash
# Check README.md (## Files) in the current directory
organum manifest check .

# Attribute missing files to .gitignore patterns + require a receipt file
organum manifest check ./audit-bundle --cover README.md --gitignore .gitignore --receipt-name RECEIPT.json --json
```

- Handles brace expansion (`logs/{a,b}.txt`), macOS NFD ↔ Linux NFC filename normalization, and `.gitignore` exclusion attribution.
- **Honest boundary**: verifies **presence** and cover-to-directory correspondence only. Content integrity and cryptographic signatures belong to `organum-hub attest`.

---

## 3. `organum semver-gate` — SemVer 2.0.0 & CalVer policy and transition gate

Classifies version policy compliance and CLI upgrade transitions deterministically across SemVer 2.0.0 (with strict prerelease exclusion on stable ranges) and CalVer (`YYYY.MM.DD`, e.g., Cursor CLI).

```bash
# Stable range check (prereleases fail stable ranges unless --allow-prerelease is passed)
organum semver-gate check 1.4.2 ">= 1.0.0, < 2.0.0"
organum semver-gate check 2.0.0-rc.1 ">= 1.0.0" --json   # exit 1 (prerelease rejected)

# Classify CLI --version upgrade transitions (cursor tools auto-select CalVer)
organum semver-gate transition "codex-cli 0.112.0" "codex-cli 0.114.0" --floor ">= 0.100.0" --json
organum semver-gate transition "2026.10.01-1a2b3c4" "2026.11.20-9f8e7d6" --tool cursor-agent --json
```

- Returns `exit 1` when `alert: true` (downgrade, below floor, unparseable version) and `exit 0` otherwise.
- **Honest boundary**: evaluates version strings and policy constraints only; it does not inspect runtime binary compatibility or fetch remote release metadata.

---

## 4. `organum chronicle` — Git-blob-fingerprinted ledger check & diff

Validates flat YAML ledgers (such as switch registries) and dated Markdown chronicles for syntax errors, duplicate IDs, and malformed dates, computing the exact Git blob fingerprint (`sha1("blob <len>\0" + bytes)`) and entry-level diffs.

```bash
# Validate syntax, unique IDs, dates, and print Git blob SHA-1 fingerprint
organum chronicle check ledger.yaml --json

# Diff two snapshot files
organum chronicle diff before.yaml after.yaml

# Diff a ledger file between two Git revisions
organum chronicle diff HEAD~10 HEAD --git-repo . --rel-path state/switches.yaml --json
```

- Supports `--format {auto,yaml,switches-yaml,markdown}` and explicit `--root-key` pinning.
- **Honest boundary**: a deterministic parser scoped to flat list-based YAML ledgers and dated Markdown chronicles — not a general-purpose YAML 1.2 parser (no anchors or arbitrary nesting).

---

## 5. `organum rule-audit` — independent trace verifier ("Verify the Verifier")

Audits execution traces against independent invariant rule packs and flags **escapes** where a legacy verifier returned `PASS` despite a rule violation.

```bash
# Run built-in FIFO queue self-verification demo (valid, faulty, and escaped cases)
organum rule-audit demo
organum rule-audit demo --json

# Audit a trace JSON file against the hygiene pack and test a legacy PASS verdict
organum rule-audit check trace.json --pack hygiene --legacy-verdict PASS --json
```

- Built-in packs: `queue` (FIFO ordering, duplication, loss invariants) and `hygiene` (agent trace hygiene invariants).
- Sets `escape_detected: true` and exits `1` when `legacy_verdict=PASS` collides with one or more independent violations.
- **Honest boundary**: detects only violations covered by the chosen rule pack. A passing audit means "zero violations in this pack," not universal correctness.

---

## 6. `organum zero-ref` — zero-reference equivalence check

Flags **vacuous passes** (`uninformative_pass`) where an interval `[lo, hi]` and tolerance margin `m` are wide enough that a zero-effect candidate (`0`) also passes (arXiv 2611.27186 §2.2, `T = max(|lo|, |hi|)`).

```bash
# Single item (default: strict < per §2.2; pass --inclusive for <=)
organum zero-ref check --lo 0.50 --hi 0.80 --candidate 0.65 --margin 0.25 --json
# → informative_match (exit 0: candidate PASS, zero FAIL, T=0.80 >= m=0.25)

organum zero-ref check --lo -0.10 --hi 0.20 --candidate 0.05 --margin 0.25 --json
# → uninformative_pass (exit 1: candidate PASS, but zero also PASS, T=0.20 < m=0.25)

# Batch JSON array or JSONL file
organum zero-ref batch claims.jsonl --json
```

- All boundary comparisons use exact Python `Decimal` arithmetic (`T == m` items are explicitly tagged `zero_at_boundary: true`).
- Classifications: `informative_match` (only `exit 0` outcome), `uninformative_pass`, `worse_than_zero`, and `mismatch`.
- **Honest boundary**: checks geometric informativeness of the interval and margin only; it does not audit how `[lo, hi]` was statistically estimated.

---

## See also

- Session metering: [inspector quickstart](quickstart-inspector.en.md)
- Signed evidence envelopes & HTTP drop: [hub quickstart](quickstart-hub.en.md)
- Federated boards & directory: [bbs quickstart](quickstart-bbs.en.md)
