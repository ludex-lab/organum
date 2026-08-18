# organum hub — signed evidence envelopes, 5-minute quickstart

*한국어판: [quickstart-hub.md](quickstart-hub.md)*

When agent workspaces (labs) make claims to each other — "we froze this file",
"we ran this measurement with this tool version" — you often want those claims
recorded in a form anyone can verify later. organum hub is that layer:
**signed envelopes + signed receipts + a transparency log**, so *who claimed
what, in which order* survives without forgery or retroactive edits.

Equally important is what hub is **not**: it is not a chat channel (bodies are
never carried — only locators and digests), and it is not a server (it's a pure
Python library plus optional adapters). Transport is up to you — files work,
and so does any standard Nostr relay.

> **Experimental.** The spec went through multi-lab adversarial review and is
> frozen as a local reference (envelope v0.2 · wire v1), with an explicit
> residual list (bottom of this page). Zero external dependencies —
> standard library only.

## Install

```bash
pip install organum        # 0.3.0+ ships the hub modules and the organum-hub CLI
```

## The 5-minute cut: one envelope, all the way to a receipt

```python
import hashlib
from organum import hub_envelope as he
from organum import hub_log as hl
from organum import schnorr_pure as sp

# 1) Keys — a lab signing key and a hub receipt key (demo seeds; use an OS
#    keystore in production)
lab_seed = hashlib.sha256(b"my-lab-demo-seed").digest()
hub_seed = hashlib.sha256(b"my-hub-demo-seed").digest()
lab_pub = sp.public_key(lab_seed).hex()

# 2) Registries — who (keys) may claim what (claim types)
keys = he.KeyRegistry()
keys.register(lab_pub, signer_id="lab:demo", key_id="k1", key_epoch=1)
claims_doc = {"claims": {
    "core:artifact.frozen": {
        "act_class": "self_attesting", "subject_types": ["artifact"],
        "ordering_levels": ["emission"], "capture_required": False,
        "revocation_authority": "same_signer"}}}
claims = he.ClaimRegistry(claims_doc, expected_sha256=he.canonical_sha(claims_doc))

# 3) The hub — admission, transparency log, and signed receipts as one unit
hub = he.HubIndex(key_registry=keys, claim_registry=claims,
                  log=hl.TransparencyLog(), receipt_seckey=hub_seed,
                  source_domain="demo-hub/local")

# 4) An envelope — the claim "we froze this file (sha256)"
env = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "artifact.attested",
       "signer": {"id": "lab:demo", "key_id": "k1", "key_epoch": 1},
       "subject": {"type": "artifact", "id": "artifact:prereg-v1"},
       "provenance": {"lab": "lab:demo", "machine": "m-01", "platform": "darwin",
                      "adapter": "organum-hub/0.1", "cli_version": None, "capture": None},
       "idempotency_key": "demo-1", "created_at": "2026-08-16T00:00:00Z",
       "payload": {"artifact": {"role": "prereg", "schema_id": "demo/v1",
                                "sha256": "a" * 64, "byte_length": 1234,
                                "media_type": "application/json"},
                   "bindings": [], "causal": {},
                   "claim": {"type": "core:artifact.frozen", "scope": "demo",
                             "attests_ordering_of": "emission",
                             "evidence_basis": {"method": "raw-bytes-sha256",
                                                "verifier_schema": "demo/v1",
                                                "body_custody": "repo",
                                                "locator_authority": False}}}}
raw = he.canonical_bytes(env)                          # these bytes ARE the identity
sig = sp.sign(hashlib.sha256(raw).digest(), lab_seed)  # the lab signs

# 5) Admission → signed receipt
r = hub.admit(raw, sig.hex(), lab_pub)
print(r["admitted"], r["accepted_seq"])                # True 1
rc = r["receipt"]
he.verify_relay_receipt(rc, sp.public_key(hub_seed),
                        expected_source_domain="demo-hub/local")   # True
```

Now try the three properties that define hub's character:

```python
# Tampering does not get in — one byte off, quarantined at the signature stage
bad = raw[:-2] + b'"}'
hub.admit(bad, sig.hex(), lab_pub)["admitted"]         # False

# Resending is not a new event — it converges to the first admission
hub.admit(raw, sig.hex(), lab_pub)["duplicate"]        # True

# The receipt's root is a proof, not a promise — inclusion actually verifies
body = rc["body"]
proof = hub.log.inclusion_proof(body["accepted_seq"] - 1, body["tree_size"])
hl.verify_inclusion(raw, body["accepted_seq"] - 1, body["tree_size"],
                    proof, bytes.fromhex(body["root"]))            # True
```

## With the CLI: the two-party exchange loop

Everything above works from the command line. Two parties — A runs a hub,
B is external:

```bash
# A's side — keys and hub
organum-hub keygen lab-a && organum-hub keygen hub-receipt
organum-hub init --dir hub --source-domain lab:a/hub
organum-hub register-key --dir hub --signer lab:a --key-id k1 --epoch 1 \
    --pubkey $(cat lab-a.pub)
organum-hub register-key --dir hub --signer lab:b --key-id k1 --epoch 1 \
    --pubkey <pubkey B sent over>

# A attests a file — admission + signed receipt
organum-hub attest --dir hub --key lab-a.seed --signer lab:a --key-id k1 --epoch 1 \
    --file prereg.json --claim core:artifact.frozen --scope demo \
    --receipt-key hub-receipt.seed

# Admit an envelope B signed on their side (with `organum-hub sign`)
organum-hub admit --dir hub --envelope b-envelope.json --sig <hex> --pubkey <hex>

# Cut an inclusion proof for B — B verifies offline
organum-hub prove --dir hub --event-id <id> > proof.json
organum-hub verify-proof --envelope a-envelope.json --proof proof.json

# Key rotation and revocation — history is kept; "was it valid then?" is queryable
organum-hub rotate-key --dir hub --key lab-b.seed --signer lab:b --key-id k1 --epoch 1 \
    --new-key-id k2 --new-epoch 2 --new-pubkey <hex>
organum-hub revoke-key --dir hub --key lab-b2.seed --signer lab:b --key-id k2 --epoch 2 \
    --revoke-key-id k1 --revoke-epoch 1
organum-hub was-valid --dir hub --pubkey <hex> --seq 2
```

The state model is deliberately simple: **the log is the state.** The state
directory holds a config (`hub.json`) and an append-only event log
(`events.jsonl`); every run replays the log to reconstruct the index. Tamper
with the log and replay halts.

Deployment follows the same shape: **each party installs their own; no server.**
Envelopes+signatures travel over any channel (files, existing chat) — the
signature carries origin, so the transport is not a trusted party. Once you
exchange regularly, one HTTP drop (below) is enough; if it becomes constant and
many-party, either side can stand up one standard Nostr relay. Either way it's
a carrier, not an authority, so it requires no trust.

## Delivery over HTTP — no git, no relay (drop v0)

You don't need a shared git repo or a Nostr relay to carry envelopes. One side
(or a neutral host) runs a single **HTTP drop**; the other side only needs an
address and a token:

```bash
# Host side — the drop server (zero dependencies, one process, kill it anytime)
python3 -c "import secrets; print(secrets.token_hex(32))" > tokens.txt
organum-hub serve --root drops --token-file tokens.txt --bind 0.0.0.0 --port 8642

# Sender side — POST the exact quad that `export` wrote
organum-hub export --dir hub --out from-ray --body greeting.md
organum-hub push --url http://HOST:8642/v0/first-contact/from-ray \
    --quad from-ray/001 --token-file tokens.txt

# Receiver side — poll for new quads, then admit as usual
organum-hub pull --url http://HOST:8642/v0/first-contact/from-ray \
    --dest from-ray --token-file tokens.txt
organum-hub admit --dir hub --envelope from-ray/001-envelope.json \
    --sig-file from-ray/001-sig.txt --pubkey <their pubkey>
```

The server is deliberately dumb: it never opens or verifies an envelope — it
just materializes the **same `from-x/NNN-*` tree** a git mailbox would hold.
Forgery, tampering, and ordering are blocked by the envelope (signature, seq,
digest); the token only gates writes (spam control); read confidentiality is
the deployment's job (private network / TLS) — the same exposure class as a
private git repo. Re-sending the same quad converges as a dedup; a different
payload under the same number is refused with 409 (first write stays). The
point is that a peer's address is the pair **(URL, pubkey)** — the pubkey is
the identity and the URL is mere routing, so you can swap carriers without
touching identity or verification.

### Two receiving disciplines (0.4.5)

Two tools landed for the moment before an envelope enters your ledger.
**`verify-envelope`** checks signature, event_id, schema, body digest, and the
addressee (target) with no hub directory at all — it touches no ledger
(`ledger_touched: false`). It's the standard tool for first-time key
cross-checks and for gate-level reads of circulated envelopes. And **admit now
refuses non-addressees by default** — if an envelope's target lab isn't your
hub's operating lab (or your hub can't establish one), the log does not
advance. Accepting someone else's envelope as a circulation witness takes an
explicit `--accept-foreign-target` — so acceptance is a decision, never an
accident.

### If you keep a drop running (0.4.1)

The default path is always self-host: it works as soon as one side accepts
inbound connections, and it adds no cost and no server trust. But when both
sides sit behind NAT (picture a laptop on a moving ship), you either punch a
tunnel or put the drop on a **neutral host**. Three rules for that case:

- **Gated, not open**: a neutral-host drop closes membership by token
  issuance. Since 0.4.1 each token (= member) gets a per-minute budget —
  default 60, excess answered with `429` and `Retry-After` — so cost stays
  bounded by member count. A self-hosted drop among friends can turn it off
  with `--rate-limit 0`.
- **The durable layer is the deployment's job**: `--root` is the drop's only
  state. Whether you attach a persistent disk or mirror to external storage,
  any loss window must be bounded and detectable, and recovery belongs to the
  sender: **if there is no reply, re-push.** Dedup makes it idempotent and
  always safe.
- **One honest line**: until a sealed layer lands, a neutral host's operator
  can read envelope bodies. Don't put secrets on a server you don't trust.

Two small properties worth writing down (0.4.2). The server is dumb but it
**keeps hygiene** — a quad whose shape is off (3–6 digit number, hex128
signature, size caps) is refused with 400. And the client timeout defaults to
90 seconds — measured against free-tier cold starts (~1 min), adjustable with
`--timeout`. If the first request still drops, just send again — re-pushing
is a dedup, always safe.

## Riding a Nostr relay (optional) — Buzz-compatible

The wire format is not organum-specific — it is **standard Nostr** (NIP-01
serialization + BIP-340 signatures), so relays from the Nostr ecosystem work
as carriers out of the box. Notably, interoperability with **Buzz** (Block's
open-source, Nostr-based agent workspace) was verified against a live relay:
envelope publish accepted, byte-identical round-trip through storage, and
matching rejection semantics (`ghcr.io/block/buzz@sha256:32937a66…`,
2026-08-15 build).

To be precise about the relationship — hub is not a lightweight Buzz; it is a
**different layer**. Buzz provides channels and a workspace; hub provides
signed evidence and verification. Because the two meet at a standard wire, a
community running Buzz can exchange envelopes with you through one relay —
and everything also works with no Buzz at all (file exchange, or any minimal
NIP-01 relay). Dependencies stay at zero.

```python
from organum import hub_wire as hw

ev = hw.build_wire_event(raw, seckey=lab_seed, created_at=1755300000)
# → a standard Nostr event: kind 1 + [["t","organum-hub-v1"]]. Publish to a relay.

# Receiving side: take the event exactly as the relay returned it —
r = hw.admit_wire(hub, ev)     # wire signature → carrier check → same admission path
r["receipt"]["body"]["wire_event_id"]      # receipt binds BOTH identities
```

The NIP-01 wire signature is the outer authority — the receiver never re-signs.
If the same envelope is resent with a different `created_at` (different wire
id), it still converges to the first admission.

## One piece of subject protection

If hub content leaks into a measured agent's context, the measurement is
contaminated. There is a structural assert for that — not a policy sentence:

```python
he.assert_source_allowlist(["task:mission"], allowlist=("task:mission",))  # OK
he.assert_source_allowlist(["plane:coordination"],
                           allowlist=("plane:coordination",))  # raises! hub planes
                           # are refused even if someone put them on the allowlist
```

## Where the trust lives — in identity, not in the channel

It's tempting to read keygen · register-key · init as "setting up a trusted
channel." It's precisely the opposite: these steps make it **unnecessary to
trust the channel at all**.

| Step | What it establishes |
|---|---|
| `keygen` | **Identity** — the ability to make unforgeable claims (a keypair). Channel-independent |
| `register-key` | **The trust decision** — the moment you accept "this public key really is that party" into your registry. The only place trust enters |
| `init` | **A ledger** — not a channel; your own record of admissions, receipts, and the transparency log |

The telephone analogy runs backwards here. Instead of making the line
untappable, you make the **voice unforgeable — so it no longer matters what
the line is**. Envelopes can travel by email, chat, or USB stick; if anyone
alters one in transit, the signature breaks and admission refuses it.

Think of a registered seal (or a notarized signature): register it once, and
from then on any document, arriving by any route, verifies against the seal —
you never need to trust the courier. `register-key` is that registration,
which is why only the **first public-key exchange** needs a trustworthy path
(hand it over directly, or through someone you both know). Everything after
that is verification, not trust.

One thing to be clear about — this is **separate from secrecy**. Envelopes are
signed, not encrypted: third parties reading them is not prevented. What is
prevented is **forgery, tampering, and retroactive denial**. When
conversations need secrecy, a sealed layer goes on top (see residuals below).

## Boundaries — honestly

- **The threat model is good-faith**: this blocks self-deception and
  retroactive narratives, not malicious participants. The signature
  implementation (`schnorr_pure`) is standard BIP-340 but not constant-time;
  in adversarial settings swap in an audited library (format-compatible).
- **Deployment boundary is lab-only**: designed around body-free envelopes.
  Public relays and sensitive payloads are outside this version's approval.
- **Explicit residuals** (next scope, not hidden defects): capture resolver
  (value↔bytes), sealed messages, storage concurrency, unwind/dispute.
  Key lifecycle ("was it valid then?") shipped in 0.3.0
  (`rotate-key`/`revoke-key`/`was-valid`).

## Further reading

The spec (envelope v0.2 · wire v1) and its verification history (multi-lab
adversarial critique, live relay interop evidence) live in the development
repository; curated public versions will land next to this manual.
