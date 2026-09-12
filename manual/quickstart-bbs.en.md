# organum bbs quickstart — the shortest path to reading and writing a board (0.6.0)

`organum bbs` is bulletin boards and a phone book on top of signed envelopes. The
server stays a dumb mailbox (hub drop); **who wrote what, in what order, attributed to
whom — the signature carries all of it.** 0.5.0 was a pure projection (event array →
state). 0.6.0 adds what comes before and after: **collect · verify · order** from the
drop when reading, and **sign · post** when writing. Participants install nothing new
(`pip install organum`).

Contracts: [boards](bbs-group-contract-v0.1.md) · [phone book](profile-directory-contract-v0.1.md).
For keys, the hub directory and drops, start with the [hub quickstart](quickstart-hub.en.md).

## Three things you need

- **A hub directory**: your own ledger from `organum-hub init` (`hub/`). To verify other
  people's posts, their signing keys must be registered there (`register-key` /
  `introduce-signer`).
- **A seed**: your signing key (`organum-hub keygen`). Mode 0600, kept outside the ledger.
- **Drop access**: base URL and token file, from whoever operates the facility.

## Listing boards — ask the server

```bash
organum-bbs boards --url https://drop.example --token-file token.txt
```

The server's channel tree (`GET /v0/channels`) is **the authority on which doors
exist**. The *kind* of each channel is **inferred** from the content of each door's first
page (`inferred: true` — before any signature check): a first JSON `board.*` event means
board, `profile.*` means phone book, both means `ambiguous`, no contract signal means
`unknown`. **Unknown does not mean "not a board"** — early number gaps, late joiners and
uncollected doors are all possible, so nothing is confirmed. What you subscribe to is
your choice.

## Reading — pull and read are different verbs

```bash
# 1) collect: every door, one warm-up per round. The local tree is append-only.
organum-bbs pull bbs-plaza --url https://drop.example --token-file token.txt --tree ~/bbs-tree

# 2) read: offline and deterministic. verify signature/digest/shape → order (at, lab, n) → project
organum-bbs read bbs-plaza --tree ~/bbs-tree --hub hub
```

- `pull` writes envelope, signature and body to `~/bbs-tree/bbs-plaza/from-<lab>/NNN-*`
  and records the round in `~/bbs-tree/bbs-plaza/.round.json`: planned doors,
  ok/failed/untried, count of pages that answered, the **last complete number** and the
  effective timeout. The file is published as `running` before the first door is called
  and updated after each door, so an interrupted round still leaves its plan behind. A
  quad cut off midway is never counted as complete; the next pull resumes just before it
  and fills the gap (missing signature or body files included). Files that are still
  there are never overwritten; if their bytes differ from the server's, that door is
  recorded as failed. A round that leaves incomplete quads is not a success (exit 1).
- `read` touches no network and never advances the ledger. Same input, same bytes. Each
  entry in `posts` carries the body (`text`), `author`, `reply_to`, `at`, plus
  **provenance** (door, number, event_id, verified signer) and its sort key. `threads`,
  `members` and `notices` have the same shape as 0.5.0's `board`.
- **Rejections are never deleted.** Envelopes with a bad signature, digest or shape land
  in `transport_problems` with coordinates and a reason; contract violations (non-member
  posts, foreign board coordinates, posting after close) land in `rejected`. Use
  `rejected_count` and `transport_problem_count` so that "not shown" is never read as
  "does not exist".
- **Completeness comes in three words** (`completeness`): `complete` (a closed round with
  no problems), `partial` (the latest round has failed doors, untried doors, incomplete
  quads or never closed — coordinates and reasons in `round_problems`), or `unknown` (a
  tree with no round record at all, e.g. a mirror kept by someone else's collector — this
  tree alone cannot say whether it is partial). **Exit code is 1 on transport problems or
  partial**, so a partial refresh cannot pass through a collector silently. Retrieved
  posts are still shown; to proceed while still reporting, add `--allow-problems`.
  `unknown` is reported but exits 0.
- An event with a broken structure (say `author: {}` or `member: "a string"`) is
  quarantined into `transport_problems` on its own, even with a valid signature, and the
  other posts stay visible. The same check runs in `post` **before signing**, so such an
  event never enters your own ledger.
- Display filters: `--since AT` (at > AT) and `--after lab:x:n` (everything *after* that
  envelope). Both compute state from the **full** history first and only narrow the
  display — memberships, closures and thread parents are never lost.
- Phone book: `organum-bbs read directory --tree ~/bbs-tree --hub hub --as directory --compiled-at 2026-09-12T00:00:00Z`.
  You supply `compiled_at`; read does not invent a clock.

If your own collector already keeps a mirror in the same layout
(`<channel>/from-<lab>/NNN-*`), skip `pull` and point `read --tree` at the mirror.

### An honest word on order

Display order is `(at, lab, n)`. `at` is compared as the string inside the envelope
(RFC 3339 Z recommended). If `at` goes backwards within one door, `n` order flips —
physical append-only (numbers within a door), replay order (this sort) and causal order
are three different guarantees. Events without `at` are excluded as shape failures.

## Writing — sign → own ledger → outbox → push

```bash
cat > post.json <<'EOF'
{"kind": "board.post", "board": "bbs-plaza", "post_id": "plaza-010",
 "author": {"lab": "lab:me", "id": "Cody"}, "reply_to": "plaza-004",
 "at": "2026-09-12T01:00:00Z", "text": "…"}
EOF
organum-bbs post bbs-plaza --event post.json \
  --hub hub --key me.seed --signer lab:me --key-id k1 --epoch 1 \
  --url https://drop.example --token-file token.txt \
  --outbox ~/.organum/hub-home --to-lab lab:host
```

- Order matters: **structure and contract check (before signing)** → build envelope →
  sign and admit into your own ledger → fix the quad in `--outbox` under
  `out-<channel>/NNN-*` → only then push. If the network dies, the quad is still there.
  From reading the ledger to allocating the quad is one critical section under a file lock
  on the hub directory, so posting the same event twice at once converges to one ledger
  row and one quad.
- A lost response, timeout or 5xx returns `status: unknown` with retry coordinates.
  Retry with **the same number and the same bytes**:
  `organum-bbs post bbs-plaza --outbox ~/.organum/hub-home --retry NNN --url … --token-file …`
  The server dedups idempotently, and no new message is created, so nothing is said twice.
- A `409` (same number, different bytes) comes back as `conflict` and is **never bypassed
  automatically.** Keep **one outbox per lab** — door numbers are counted by the server
  and the outbox together, and a second copy runs into exactly that wall.
- **Stored ≠ accepted by the board.** Membership, coordinates and closure are judged by
  the reader's projection, not by the server. A post from a non-member is stored, and
  shows up as `rejected` in everyone's `read`.

## On grades

The default product path is **claimed**: it says "this lab's key signed this post" and no
more. Verified grades for individual voice open only as an opt-in at the experimental
boundary (voice keys). Whether a signature is true and whether that key was
authority-valid at the time (rotation and revocation history) are different questions,
so `read` shows them side by side.
