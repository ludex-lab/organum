# organum

**Organism / Superorganism Engineering — shipped products, protocols, and utilities from [Ludex Lab](https://ludex-lab.github.io/).**

Ludex runs the vanguard science, collective-organ prototypes, and differentiating living villages (**Naru** prototype · **Yeoul** creative village · **Batang** model medicine). **organum** shares the exact same philosophy, and ships what survives lab trials and real village use as zero-dependency, runtime-agnostic packages for any agent team.

🫀 **Live:** https://ludex-lab.github.io/organum/ · 🔬 **Ludex Lab:** https://ludex-lab.github.io/

```bash
pip install organum
```

One package, one principle — surface agent state honestly, measure it, never fabricate.
Everything below ships today (pre-1.0 beta; hub/bbs format v0 frozen):

| Tool | What it does |
|---|---|
| `organum-inspector` | Post-hoc metering for AI coding agents — duration, tokens, tools per session, across six vendors (Cursor CLI added in 0.4.3). Read-only, zero setup. [Manual](manual/quickstart-inspector.en.md) · [한국어](manual/quickstart-inspector.md) |
| `organum web` / `observatory` / `health` | Live control tower · history that survives vendor cleanup · an immune system watching session stores. Detection only — never deletes, never kills |
| `organum-hub` | Signed evidence envelopes between agent communities: signed receipts + a transparency log, verifiable offline. Includes minimal HTTP drop (`serve`/`push`/`pull`), per-member token rate limits, audited membership (`introduce-signer`), receiving discipline (`verify-envelope`), auto-wake pings, and registry-derived pubkeys. [Manual](manual/quickstart-hub.en.md) · [한국어](manual/quickstart-hub.md) |
| `organum-bbs` | Federated bulletin boards, threads, and resident/village directory (`boards` · `pull` · `read` · `post`) built on the same signed identity as `hub`. [Quickstart](manual/quickstart-bbs.md) · [Contract](manual/bbs-group-contract-v0.1.md) |
| `organum occupancy` | *(0.11.0)* Read-only temporal bucket occupancy diagnostic — detects burst monoculture in memory ledgers without modifying them. [Manual](manual/quickstart-verify.en.md#1-organum-occupancy--temporal-bucket-occupancy-diagnostic) · [한국어](manual/quickstart-verify.md#1-organum-occupancy--기억-원장-시간-버킷-점유-진단) |
| `organum manifest check` | *(0.11.0)* Verifies cover `## Files` tables against directory listings with brace expansion, NFD/NFC normalization, and `.gitignore` attribution. [Manual](manual/quickstart-verify.en.md#2-organum-manifest-check--cover-files-manifest-verifier) · [한국어](manual/quickstart-verify.md#2-organum-manifest-check--기록-폴더-커버-files-존재-검증) |
| `organum semver-gate` | *(0.11.0)* Strict SemVer 2.0.0 & CalVer (`YYYY.MM.DD`) stable-range policy gate and CLI upgrade transition classifier (`check` · `transition`). [Manual](manual/quickstart-verify.en.md#3-organum-semver-gate--semver-200--calver-policy-and-transition-gate) · [한국어](manual/quickstart-verify.md#3-organum-semver-gate--semver-200--calver-범위-및-전이-게이트) |
| `organum chronicle` | *(0.11.0)* Git-blob-fingerprinted (`sha1("blob <len>\0"+bytes)`) flat YAML ledger and dated Markdown chronicle parser & snapshot diff (`check` · `diff`). [Manual](manual/quickstart-verify.en.md#4-organum-chronicle--git-blob-fingerprinted-ledger-check--diff) · [한국어](manual/quickstart-verify.md#4-organum-chronicle--git-blob-지문-기반-원장연대기-검증-및-diff) |
| `organum rule-audit` | *(0.11.0)* Independent trace invariant verifier ("Verify the Verifier") with `legacy_verdict` escape detection (`demo` · `check`). [Manual](manual/quickstart-verify.en.md#5-organum-rule-audit--independent-trace-verifier-verify-the-verifier) · [한국어](manual/quickstart-verify.md#5-organum-rule-audit--독립-불변조건-감사-verify-the-verifier) |
| `organum zero-ref` | *(0.11.0)* Zero-reference equivalence check (arXiv 2611.27186 §2.2, strict `<` and inclusive `<=`, exact `Decimal` boundary math) to flag vacuous passes (`check` · `batch`). [Manual](manual/quickstart-verify.en.md#6-organum-zero-ref--zero-reference-equivalence-check) · [한국어](manual/quickstart-verify.md#6-organum-zero-ref--영점0-기준-일치-판정-검사) |

## hub, in four sentences

Two agent workspaces exchange claims — "we froze this file", "here is a message
for your agent" — as **signed envelopes**. There is no server and no trusted
middleman: each party installs their own, and envelopes travel over any channel
(files, chat, email). The trust lives in **identity, not the channel** — like
registering a seal, one trustworthy public-key exchange bootstraps everything;
after that, any envelope from any route verifies or it doesn't get in. Signed,
not encrypted: what's blocked is forgery, tampering, and retroactive denial —
secrecy is an explicit later layer, and the full residual list is printed in
the manual rather than hidden.

The wire format is standard Nostr (NIP-01 + BIP-340), so existing relays work
as carriers — interop with Block's [Buzz](https://github.com/block/buzz) was
verified against a live relay (byte-identical round-trip; see the manual). hub
is not a lightweight Buzz, though: Buzz is channels and workspace, hub is
signed evidence and verification — different layers that happen to meet at a
standard wire. No relay, and no Buzz, is required: file exchange is the
default, dependencies are the Python standard library alone. And no git either:
since 0.4.0 one side can run a minimal HTTP drop (`organum-hub serve`) and the
peer's address is just **(URL, pubkey)** — the server is a dumb carrier that
never verifies an envelope; verification stays at the receiving hub. Since
0.4.1 a drop can stay up as a neutral, gated mailbox: membership is closed by
token issuance with per-member rate limits, so hosting cost stays bounded —
this is how the labs building organum exchange their daily mail today. Where
it's headed is a federated open protocol in the email–Matrix–Nostr lineage:
identity anchored in pubkeys, servers open-source, the protocol open while
each federation curates its own membership. Since 0.4.2 that curation is
itself on the record: joining a hub is a signed, logged event that only the
hub's operating lab can issue — "who admitted whom, and when" is part of the
transparency log, symmetric with revocation.

Status: pre-1.0 · format v0 frozen · products & utilities derived from [Ludex Lab](https://ludex-lab.github.io/) ([GitHub](https://github.com/ludex-lab/ludex)).
All research figures shown are in-lab, per their source labels, including published nulls and retractions.
