# organum-inspector — a 5-minute quickstart

*한국어: [quickstart-inspector.md](quickstart-inspector.md)*

You gave two agents the same task — which one was faster, and what did each
actually consume? How many tokens did yesterday's work really cost? organum-
inspector answers that **after the fact**: it reads the session records your
agent CLIs already leave on disk and prints duration, tokens, tool calls, and
files per session. Nothing to set up, nothing written to your project.

> pre-1.0 (beta) — the format is still moving.

## Install

```bash
pip install organum        # or: pipx install organum
```

## The one-minute version: post-hoc metering

Point it at any project folder.

```bash
cd ~/my-project
organum-inspector .
```

```
━ organum inspector · my-project · window 45d · 2 sessions
  vendor    model         start          duration       in     out   cache tools files
  codex     gpt-5.6-sol   07-15 10:14    3.4h    34.2M   64.3K   32.3M   430     8
  grok      grok-4.5      07-15 12:10    17.8m    116K       —       —   179    37
```

**The terminal being measured needs nothing done to it.** Your agent CLI writes
its records anyway; inspector only reads them. No `init` required, and it works
on sessions you finished last week — that's what "post-hoc" means.

Not just Claude Code either — Codex, Gemini (Antigravity), Grok, and OpenCode
sessions normalize into the same table.

A `—` where a number should be isn't zero — it means the vendor doesn't record
that value on disk. Token semantics differ per vendor, so the safe cross-vendor
axes are **duration, tools, and files**.

## See it in a browser, share it: `--html`

Prefer a browser view over the terminal table, or want to hand it to someone?

```bash
organum-inspector . --html report.html
```

You get a single self-contained HTML file — timeline (vendor-colored bars),
session table, vendor comparison bars — that opens without a server. It's a
file: drop it in a channel or keep it as a record. For machines, `--json` feeds
your analysis pipeline directly.

## A real case

We gave Codex and Grok the exact same design task; Grok finished 10× faster.
But quality? Three cross-reviewers (commissioner, winner, loser) ruled
unanimously for Codex — cost measured by inspector, quality judged by the
agents, and the choice stopped being taste. Full story:
[case-study-inspector-duel.en.md](case-study-inspector-duel.en.md).

## Going further — live & history (beta)

Inspector is purely "meter finished work," but the same `pip install organum`
ships the rest of the observation suite. Still-in-beta, but it all runs:

- **`organum web`** — a live control tower. Every currently-running session as a
  card in the browser (model, tokens, lineage), in real time. It self-retires
  after two idle hours (`--idle-timeout 0` to disable).
- **`organum observatory`** — history accumulation. Snapshots pile up before the
  vendor deletes session records (within weeks), so you can see trends, model
  mix, and cost past 30 days. `organum init` once, then `observatory sync` /
  `observatory report [--html]`.

Both are covered in [quickstart-observe.en.md](quickstart-observe.en.md).

## The boundary, stated once

organum never starts, stops, or routes sessions. Read-only metering is all it
does, so attaching it to any workflow breaks nothing. Inspector writes nothing
to the target folder; anything observatory accumulates lives in that project's
`.organum/` and stays out of git by default.

## Common questions

**Q. Can I measure the project I'm working in right now?**
Yes — `organum-inspector .` there shows the sessions up to a moment ago.

**Q. Are subagent tokens included?**
Yes. Subagents appear as their own rows and count in the totals. Parent
sessions alone miss a large share of real consumption — closing that gap is one
of this tool's reasons to exist.

**Q. My old sessions don't show up.**
Inspector reads within a discovery window (45 days by default; `--window` to
change), provided the vendor hasn't cleaned the transcripts yet. Long-term
retention is observatory's job (`sync`).

**Q. I renamed or moved the folder.**
Old-path sessions aren't found automatically (records are path-keyed). In
observatory, fold them in with `organum observatory sync --also ~/old/project`.
