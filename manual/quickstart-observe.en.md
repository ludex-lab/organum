# Watching your agent — a 5-minute quickstart

Sooner or later, working with Claude Code, the questions arrive. How many tokens
am I burning right now? How many subagents just spawned — and on which model did
they run? organum is an observation tool that answers them. It doesn't steer or
modify your agent; it reads the session records already landing on disk and
gathers them into one view.

> pre-1.0 — the format is still moving.

## Install

```bash
pip install organum        # or: pipx install organum
```

## The one-minute version: raise the control tower

One line, from the project folder you want to watch:

```bash
cd ~/my-project
organum web        # → http://localhost:7332
```

Open the browser and every session running in that project appears as a card:
model, tokens (in/out/cache), tool-use breakdown, last activity. When a session
spawns a subagent, a separate card appears with a `subagent ← parent-id` chip.
Model mixes — your main session on one model quietly running its explorer
subagents on another — become visible here for the first time.

**The terminal being observed needs nothing done to it.** Claude Code writes its
session records to `~/.claude/projects/` anyway; organum only reads them. It
works on the terminal you're using right now, or on a project that has never
heard of organum. To watch a second project, launch another tower from that
folder with `organum web --port 7333`.

It's not just Claude Code, either — Codex, Gemini (Antigravity), Grok, and
OpenCode sessions converge onto the same screen as the same kind of card.

## Accumulating statistics: seeing past 30 days

Session records come with a catch: Claude Code deletes old transcripts after
roughly a month. Questions like "how many tokens did this project burn last
quarter" can only be answered if snapshots were taken before the records
vanished. That's what observatory does.

```bash
cd ~/my-project
organum init                 # create the .organum/ state folder (once)
organum observatory sync     # snapshot every discoverable session
organum observatory stats --by model
```

```
observatory — last 30 days · 16 sessions (terminal 11 · subagent 5)
  tokens: in 918.4K · out 943.6K · cache 103.1M
  --by model:
    claude-fable-5               10 sessions · in 1.9K · out 881.1K · cache 100.6M
    claude-haiku-4-5-20251001     2 sessions · in 566 · out 25.5K · cache 2.1M
    ...
```

After one `init` it's mostly automatic: the tower records while it's open, and
every `organum checkup` sweeps. Re-running `sync` never duplicates. Slice with
`--by role`, `--by origin`, `--by vendor`.

For "now vs history" in one view, there's a report:

```bash
organum observatory report
```

Live sessions, today, and history (daily trend, model mix, largest sessions)
come as separate bands. Project consumption tends to be dominated by a rare
marathon session — without this separation, the current screen badly
understates the true scale.

Prefer a browser view, or want to share it? Use `--html`:

```bash
organum-inspector . --html report.html          # post-hoc metering report
organum observatory report --html report.html   # now/history band report
```

You get a single self-contained HTML file — timeline, session table, vendor
comparison bars — that opens without a server. It's a file: drop it in the team
channel or keep it as a record.

If you ever see `—` where a number should be, that's not zero — it means the
vendor doesn't record that value on disk. organum never flattens the unknown
into zero.

## Naming your sessions (optional)

Cards read better with a name and an intent instead of a session hash. One line
in the agent's terminal:

```bash
organum join --role dev --intent "refactor the payment module" --for mycell
```

Now the card shows `mycell · dev` with the intent, and `stats --by role` groups
consumption by what the session was *for*. Skip it and observation still works —
identity is opt-in.

## The boundary, stated once

organum never starts, stops, or routes sessions. Read-only observation and
state accumulation are all it does, which is why attaching it to any workflow
breaks nothing. Everything it accumulates lives locally in that project's
`.organum/` and stays out of git by default (sharing is your explicit choice).

## Common questions

**Q. Can I watch the Claude Code terminal I'm working in right now?**
Yes. Launch `organum web` from the same folder (in the background, or from
another terminal) and your current session appears as a card. You can even ask
the agent itself to run it.

**Q. Are subagent tokens included?**
Yes. Subagents appear as their own cards and are included in the totals. Parent
cards alone miss a large share of real consumption — closing that gap is one of
this tool's reasons to exist.

**Q. My old sessions don't show up.**
The tower shows the last 30 minutes of activity as "live". For past sessions
use `organum observatory stats` — provided a `sync` or `checkup` ran at least
once before the transcripts were cleaned (about 30 days).
