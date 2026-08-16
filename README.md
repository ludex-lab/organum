# organum

**Organism Engineering** — a cognition & discipline layer for the agents you already run:
persistent memory, differentiation, immunity (guard), and coordination without a dispatcher.

🫀 **Live:** https://ludex-lab.github.io/organum/

```bash
pip install organum
```

One package, one principle — surface agent state honestly, measure it, never fabricate.
Everything below ships today (pre-1.0 beta; formats still moving):

| Tool | What it does |
|---|---|
| `organum-inspector` | Post-hoc metering for AI coding agents — duration, tokens, tools per session, across five vendors. Read-only, zero setup. [Manual](manual/quickstart-inspector.en.md) · [한국어](manual/quickstart-inspector.md) |
| `organum web` / `observatory` / `health` | Live control tower · history that survives vendor cleanup · an immune system watching session stores. Detection only — never deletes, never kills |
| `organum-hub` | **New: 0.4.x** — signed evidence envelopes between agent communities: signed receipts + a transparency log, verifiable offline. 0.4.0 adds git-less delivery: a minimal HTTP drop (`serve`/`push`/`pull`). [Manual](manual/quickstart-hub.en.md) · [한국어](manual/quickstart-hub.md) |

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
never verifies an envelope; verification stays at the receiving hub.

Status: pre-1.0 · incubating under the [Ludex lab](https://github.com/ludex-lab/ludex).
All research figures shown are in-lab, per their source labels, pending third-party
replication.
