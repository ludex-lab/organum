# Case study — same task, two agents: what post-hoc metering reveals

*2026-07-15, ludex-design. Raw data: [case-study-inspector-duel.json](case-study-inspector-duel.json).
Instrument: `organum-inspector` (read-only post-hoc metering). Korean original:
[case-study-inspector-duel.md](case-study-inspector-duel.md).*

## Setup

JJ gave Codex (gpt-5.6-sol) and Grok (grok-4.5) the **exact same design task**
(two game tile-set skins: cozy-dusk and sunny-suburb). Both delivered, and the
felt difference was obvious — "Grok finished remarkably fast; Codex took ages."
Afterwards, the agents cross-evaluated each other's outputs.

organum did nothing inside this folder. Both CLIs leave session records in
their own home directories; every number below was measured **after the work
was already done**.

## Phase 1 — the task

| | Grok grok-4.5 | Codex gpt-5.6-sol |
|---|---|---|
| Duration | **17.8 min** | **178.2 min (10.0×)** |
| Input tokens | 116.1K | 34.2M (32.3M cache re-reads; ~1.85M fresh) |
| Output tokens | — (not recorded on disk) | 64.3K |
| Tool calls | 179 | 430 (function 279 + custom 151) |
| Patches applied | — | 7 |
| Files touched | 37 | 8 |

The felt gap ("Grok was way faster") quantifies to exactly 10.0×, and the
**working styles** separate cleanly:

- **Grok**: `image_gen 48 · image_edit 33 · terminal 52 · read 31` — divergent:
  generating design artifacts directly as images, spreading across 37 files.
- **Codex**: convergent — three hours, 430 calls, carving 8 files through 7
  patches, with per-call context re-reads inflating input to 34M (a 195MB
  rollout file).

## Phase 2 — cross-evaluation (after the task)

| | Grok | Codex |
|---|---|---|
| Span | +68.2 min (wall, includes idle) | +24.7 min |
| Input delta | +15.6K | +2.13M (cache +2.03M) |
| Tool calls | +11 (terminal +5 · read +6) | +12 (custom only) |
| Output | +6 files | +2 patches · +2 files |

Both evaluated lightly (~a dozen calls). One asymmetry stands out: **Codex kept
patching even while evaluating** (fix-as-you-review), while Grok read and added
files.

## Phase 3 — quality (the evaluation matrix)

Three evaluators — the commissioning Claude, the winner, and the loser — plus
the human user.

**The commissioner** (8 comparison sheets + tiling checks + 2 in-game board
compositions): cozy-dusk → *clear Codex win* (39 tiles cohere into one world;
Grok pack had unremoved background remnants, intra-pack style drift, palette
split). sunny-suburb → *Codex ahead, Grok fighting well* (Grok's contract
compliance was actually better, but its houses converged on near-identical
boxes in a "house = character" system).

**The user (JJ)**: "Grok simple, Codex detailed — honestly a smaller gap than
expected; a matter of taste."

**Codex** (blind A/B protocol, self-designed: random assignment, scores locked
before identities revealed; rubric 20/25/20/20/15): Cozy 94 vs 75, Sunny 95 vs
79 — mean **94.5 vs 77.0**. Included genuine self-criticism, and separated
technical checks (both passed) from quality judgment.

**Grok** (the most quantitative: measurable items first — per-house color
stdev 17.5 vs 25.6 cozy, 28.9 vs 59.3 sunny; fill density): verdict **Codex
wins both skins**, 8.2 vs 5.8 overall. Flagged a suspected file/motif mixup in
its *own* pack, listed its own advantages fairly, and closed with "ship Codex's
output; mine is a valid alternate draft."

### What the closed matrix shows

- **Direction unanimous — including the loser.** The strongest possible signal:
  the loser argued its own defeat in the most detail.
- **Perceived margin varies asymmetrically**: user (taste-level) < commissioner
  < Grok on itself (2.4/10) < Codex on itself (17.5/100). **The winner was most
  generous to its own work; the loser harshest on its own** — self-evaluation
  bias is not symmetric.
- **A measurement-layer contradiction**: Grok's automated check reported 0px of
  chroma-key residue; two visual evaluators saw remnants. Passing automated
  contract checks ≠ visual cleanliness — with the contract layer tied, all
  quality differentiation happened above the contract.
- **Methodological diversity emerged for free**: in-game composition
  (commissioner), blind A/B + rubric (Codex), pixel metrics (Grok) — three
  different methods, one direction.

## Cost × quality, together

| | Grok grok-4.5 | Codex gpt-5.6-sol |
|---|---|---|
| Cost | 17.8 min · in 116K | 178.2 min (10.0×) · in 34.2M |
| Contract (automated) | pass — tied | pass — tied |
| Quality (3-way review) | loses — unanimous (incl. own 5.8/10) | **wins — unanimous** |
| User perception | simple | detailed (gap: taste-level) |

**The lesson**: 10× the cost bought a **unanimous quality win** — but the
contract layer was tied and the user-felt gap was small. So the conclusion is
economics, not ranking: if passing the contract is the goal, the Grok-type does
it at a tenth of the price; if world-cohesion hero assets are the goal, the
Codex-type earns its 10×. Grok's own phrase is exact — *"a valid alternate
draft."* When cost is measured by a tool and quality by cross-review, the
choice stops being taste and becomes data.

## Method notes (reproduce it)

```bash
pip install organum
organum-inspector ~/path/to/project          # read-only, no init
organum-inspector ~/path --json              # for your analysis pipeline
organum-inspector ~/path --html report.html  # self-contained shareable report
```

- Phase boundary = each session's last event timestamp at first measurement;
  per-event UTC timestamps make post-hoc phase decomposition possible.
- Three honesty caveats: ① Grok's `—` for out/cache means *not recorded*, not
  zero ② token semantics differ per vendor (Codex counts cumulative per-call
  totals) — duration, tools, and files are the safe cross-vendor axes ③ model
  and harness both differ, so this compares **combinations**.
- Vendor transcripts are perishable (cleaned within weeks). That is why this
  case survives as a document — and why organum observatory (accumulation) and
  inspector (post-hoc metering) exist.
