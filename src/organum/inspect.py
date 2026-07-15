"""organum inspect — 라이브 유기체 vitals (Claude Code transcript tail, stdlib only).

경계: **read-only 관찰.** 작업은 네이티브 CLI에서 일어난다 — 이 창은 활력징후만 본다.
데이터 출처(훅·wrapping 불필요): Claude Code가 이미 쓰는 세션 transcript
  ~/.claude/projects/<cwd-mangled>/<active>.jsonl
  (assistant.message.usage=토큰 · content[].tool_use=도구 · attributionSkill=스킬 ·
   message.model / system.fallbackModel=브레인·폴백)
+ 프로젝트 로컬 .organum/ (map·guard·meta).

첫 슬라이스(MVP): 대사(토큰)·브레인·스킬/도구·활동·지도·면역. 비용은 가격 미설정 시 토큰만
표시한다 (measured≠asserted — 모르는 단가를 지어내지 않는다).
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import time
import unicodedata
from collections import Counter, deque
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}

# 모델별 $/Mtok — 사용자가 채운다(모르면 비움). 미설정이면 비용 대신 토큰만 표시.
# measured≠asserted: 단가는 바뀌고 벤더마다 달라 — 지어내지 않는다.
PRICES: dict[str, dict[str, float]] = {
    # "claude-opus-4-8": {"in": 15.0, "out": 75.0, "cache_read": 1.5, "cache_write": 18.75},
}


def _mangle(cwd: Path) -> str:
    return str(cwd.resolve()).replace("/", "-")


def find_transcript(cwd: Path, override: str | None = None) -> Path | None:
    """현재 cwd의 활성 세션 transcript(최신 mtime)를 찾는다. 없으면 None."""
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    d = CLAUDE_PROJECTS / _mangle(cwd)
    if not d.is_dir():
        return None
    js = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return js[-1] if js else None


def find_all_transcripts(cwd: Path, window_min: float = 30.0) -> list[Path]:
    """work-site(cwd)의 *활성* 세션 transcript 전부 — mtime이 window_min분 내 = 살아있는 세포.

    각 세션 jsonl = 그 세포의 **관점-로컬 기록**. 인스펙터는 이들을 read-only로 *수렴*한다 —
    단일 가변 파일을 공유·클로버링하는 게 아니라(§2.1-⑤ bonds 패턴). 조율자도 디스패처도 없다.
    """
    d = CLAUDE_PROJECTS / _mangle(cwd)
    if not d.is_dir():
        return []
    now = time.time()
    out = []
    # 최상위 = 터미널 세션. <세션uuid>/subagents/agent-*.jsonl = 그 세션이 spawn한 서브에이전트 —
    # 별도 transcript라 여기서 안 주우면 토큰·툴이 통째로 관측 밖(부모엔 Agent 칩 1개만 남는다).
    for p in list(d.glob("*.jsonl")) + list(d.glob("*/subagents/*.jsonl")):
        try:
            if now - p.stat().st_mtime <= window_min * 60:
                out.append(p)
        except OSError:
            continue
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def ts_age_seconds(last_ts: str | None, now: float | None = None) -> float | None:
    """마지막 *내용* 레코드 timestamp 이후 경과 초. **파일 mtime이 아니라 실제 에이전트 활동 기준**
    — Claude Code 앱이 세션 파일 mtime만 건드려 유령을 live로 오판하는 것을 막는다. 파싱 실패 시 None."""
    if not last_ts:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(last_ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        return None
    return (time.time() if now is None else now) - t


class Vitals:
    """transcript를 증분 tail하며 누적하는 활력징후. 파일 교체(새 세션) 감지 시 리셋."""

    def __init__(self) -> None:
        self.in_tok = self.out_tok = self.cache_read = self.cache_create = 0
        self.tools: Counter = Counter()
        self.skills: Counter = Counter()
        self.model: str | None = None
        self.fallback = False
        self.branch: str | None = None
        self.entrypoint: str | None = None
        self.files: set[str] = set()
        self.assistant_msgs = 0
        self.recent: deque = deque(maxlen=8)
        self.first_ts: str | None = None
        self.last_ts: str | None = None
        self._offset = 0
        self._buf = ""

    def update(self, path: Path) -> bool:
        """새 이벤트를 반영. 새로 읽은 게 있으면 True (하트비트 idle 리셋용)."""
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size < self._offset:  # 파일이 줄었다 = 새 세션 파일 → 리셋
            self.__init__()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError:
            return False
        data = self._buf + chunk
        parts = data.split("\n")
        self._buf = parts.pop()  # 마지막 불완전 라인 보류
        new = False
        for line in parts:
            line = line.strip()
            if line:
                self._ingest(line)
                new = True
        return new

    def _ingest(self, line: str) -> None:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        t = ev.get("type")
        if ev.get("timestamp"):
            if self.first_ts is None:
                self.first_ts = ev["timestamp"]
            self.last_ts = ev["timestamp"]
        if ev.get("gitBranch"):
            self.branch = ev["gitBranch"]
        if ev.get("entrypoint") and not self.entrypoint:  # cli=사람 터미널 · sdk-cli=spawn된 subagent
            self.entrypoint = ev["entrypoint"]
        if t == "assistant":
            msg = ev.get("message") or {}
            u = msg.get("usage") or {}
            self.in_tok += u.get("input_tokens", 0) or 0
            self.out_tok += u.get("output_tokens", 0) or 0
            self.cache_read += u.get("cache_read_input_tokens", 0) or 0
            self.cache_create += u.get("cache_creation_input_tokens", 0) or 0
            if msg.get("model"):
                self.model = msg["model"]
            if ev.get("attributionSkill"):
                self.skills[ev["attributionSkill"]] += 1
            self.assistant_msgs += 1
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    self.tools[name] += 1
                    inp = b.get("input") or {}
                    if name in FILE_TOOLS and inp.get("file_path"):
                        self.files.add(inp["file_path"])
                    arg = inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""
                    self.recent.append((name, str(arg).replace("\n", " ")[:58]))
        elif t == "system":
            if ev.get("fallbackModel"):
                self.fallback = True

    def cost(self) -> float | None:
        pr = PRICES.get(self.model or "")
        if not pr:
            return None
        return (
            self.in_tok * pr["in"]
            + self.out_tok * pr["out"]
            + self.cache_read * pr["cache_read"]
            + self.cache_create * pr["cache_write"]
        ) / 1_000_000


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _dwidth(s: str) -> int:
    """표시 폭 — CJK/전각은 2칸 (좁은 pane 정렬용)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _trunc(s: str, w: int) -> str:
    """표시 폭 w에 맞춰 자르고, 잘리면 … 붙임."""
    if _dwidth(s) <= w:
        return s
    out, cur = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if cur + cw > w - 1:
            break
        out.append(c)
        cur += cw
    return "".join(out) + "…"


def _term_width() -> int:
    """실제 터미널(=tmux pane) 폭. shutil은 COLUMNS env를 우선하는데 부모 셸의 전체-창 폭이
    pane에 상속되면 틀린다 → pty fd에서 직접 ioctl로 읽는다. 파이프면 shutil로 폴백."""
    for fd in (1, 2, 0):  # stdout · stderr · stdin
        try:
            w = os.get_terminal_size(fd).columns
            if w > 0:
                return w
        except (OSError, ValueError):
            continue
    return shutil.get_terminal_size((84, 24)).columns


def _read_organum(state_dir: Path | None) -> dict:
    """map 커버리지·guard 최근·meta agent_model 을 best-effort로."""
    out: dict = {"agent_model": None, "map": None, "guard": [], "agent": None}
    if state_dir is None:
        return out
    try:
        from organum import state as st

        meta = st.load_meta(state_dir)
        out["agent_model"] = meta.get("agent_model")
        out["agent"] = meta.get("agent")
        rm = st.load_repo_map(state_dir)
        if rm:
            files = [n for n in rm["nodes"].values() if n.get("kind") == "file"]
            read = sum(1 for n in files if n.get("status") == "read")
            out["map"] = (read, len(files))
    except Exception:
        pass
    g = state_dir / "guard.jsonl"
    if g.is_file():
        try:
            lines = [l for l in g.read_text(encoding="utf-8").splitlines() if l.strip()]
            out["guard"] = lines[-3:]
        except OSError:
            pass
    return out


def render(v: Vitals, cwd: Path, state_dir: Path | None,
           spinner: str = "", idle: int | None = None) -> str:
    cols = _term_width()
    W = max(24, min(cols, 100) - 1)  # -1: 좁은 pane 엣지 wrap 방지 여유 (JJ)
    org = _read_organum(state_dir)
    L: list[str] = []

    def rule(title: str) -> None:
        base = f"─ {title} "
        L.append(_trunc(base + "─" * max(0, W - _dwidth(base)), W))

    def line(s: str) -> None:
        L.append(_trunc(s, W))  # 모든 내용 줄을 pane 폭에 맞춰 자름 (soft-wrap 방지)

    proj = cwd.name
    branch = f" · ⌥{v.branch}" if v.branch else ""
    L.append("")  # 상단 여백 (첫 줄이 pane 위에 딱 붙지 않게)
    line(f"◉ organum · {proj}{branch}")
    L.append("")

    rule("brain 브레인")
    flags = []
    if v.fallback:
        flags.append("⚠fallback")
    if org["agent_model"] and v.model and org["agent_model"] != v.model:
        flags.append(f"⚠label({org['agent_model']})≠live")
    line(f"  live: {v.model or '—'}" + ("  " + " ".join(flags) if flags else ""))

    rule("metabolism 대사 · 토큰")
    total_in = v.in_tok + v.cache_read
    hit = (v.cache_read / total_in * 100) if total_in else 0.0
    line(f"  out {_fmt(v.out_tok)} · in {_fmt(v.in_tok)} · cache {_fmt(v.cache_read)} ({hit:.0f}%)")
    c = v.cost()
    cost_s = f"≈${c:.2f}" if c is not None else "$ 미설정"
    line(f"  turns {v.assistant_msgs} · tools {sum(v.tools.values())} · {cost_s}")

    rule("skills · tools")
    if v.skills:
        line("  skills: " + " · ".join(f"{k}×{n}" for k, n in v.skills.most_common(3)))
    line("  tools: " + (" · ".join(f"{k}×{n}" for k, n in v.tools.most_common(6)) or "—"))

    rule("map 지도 · frontier")
    touched = len(v.files)
    if org["map"]:
        read, tot = org["map"]
        line(f"  touch {touched} · read {read}/{tot} · frontier {tot - read}")
    else:
        line(f"  touch {touched} · (.organum 없음 — organum init)")

    rule("activity 활동")
    if v.recent:
        for name, arg in list(v.recent)[-6:]:
            line(f"  {name:<8} {arg}")
    else:
        line("  —")

    if org["guard"]:
        rule("immune 면역")
        for g in org["guard"]:
            line("  " + g)

    L.append("")
    ts = v.last_ts or "—"
    tshort = ts[11:16] if len(ts) >= 16 else ts  # HH:MM
    if spinner:
        foot = f"─ {spinner} idle {idle}s · {tshort} · ^C "
    else:
        foot = f"─ {tshort} · read-only "
    L.append(_trunc(foot + "─" * max(0, W - _dwidth(foot)), W))
    return "\n".join(L)


def render_multi(cells: dict, cwd: Path, state_dir: Path | None,
                 spinner: str = "", idle: int | None = None) -> str:
    """여러 세포(세션)를 한 화면에 수렴 — 집계 + 세포별 컴팩트. 관점-로컬 read-only (§2.1-⑤)."""
    cols = _term_width()
    W = max(24, min(cols, 100) - 1)
    L: list[str] = []

    def line(s: str) -> None:
        L.append(_trunc(s, W))

    def rule(title: str) -> None:
        base = f"─ {title} "
        L.append(_trunc(base + "─" * max(0, W - _dwidth(base)), W))

    order = sorted(cells.items(), key=lambda kv: kv[1].last_ts or "", reverse=True)
    L.append("")
    line(f"◉ organum · {cwd.name} · {len(order)} cells 세포")
    L.append("")
    rule("swarm 집계 · 한 현장 read-only 수렴")
    s_out = sum(v.out_tok for _, v in order)
    s_cache = sum(v.cache_read for _, v in order)
    s_tools = sum(sum(v.tools.values()) for _, v in order)
    line(f"  Σ out {_fmt(s_out)} · cache {_fmt(s_cache)} · tools {s_tools}")
    for path, v in order:
        cid = Path(path).stem[:8]
        ts = (v.last_ts or "")[11:19]
        rule(f"cell {cid} · {v.model or '—'}")
        line(f"  out {_fmt(v.out_tok)} · tools {sum(v.tools.values())} · touch {len(v.files)} · {ts}")
        if v.recent:
            name, arg = v.recent[-1]
            line(f"  ▸ {name} {arg}")
    L.append("")
    if spinner:
        foot = f"─ {spinner} idle {idle}s · {len(order)} cells · ^C "
    else:
        foot = f"─ {len(order)} cells · read-only "
    L.append(_trunc(foot + "─" * max(0, W - _dwidth(foot)), W))
    return "\n".join(L)


def _run_multi(cwd: Path, state_dir: Path | None, once: bool = False, interval: float = 1.0) -> int:
    cells: dict[str, Vitals] = {}

    def refresh() -> bool:
        paths = find_all_transcripts(cwd)
        active = {str(p) for p in paths}
        new = False
        for p in paths:
            if cells.setdefault(str(p), Vitals()).update(p):
                new = True
        for k in list(cells):  # 창 밖으로 나갔거나 *내용*이 30분 넘게 조용한(ghost) 세포 제거
            age = ts_age_seconds(cells[k].last_ts)
            if k not in active or age is None or age > 1800:
                del cells[k]
        return new

    if once:
        refresh()
        if not cells:
            print("organum inspect --all: 이 폴더에 활성 세션(세포)이 없음.")
            return 1
        print(render_multi(cells, cwd, state_dir))
        return 0

    frames = "|/-\\"
    i = 0
    last_new = time.monotonic()
    try:
        while True:
            if refresh():
                last_new = time.monotonic()
            idle = int(time.monotonic() - last_new)
            spin = frames[i % len(frames)]
            i += 1
            if cells:
                print("\033[2J\033[H" + render_multi(cells, cwd, state_dir, spinner=spin, idle=idle),
                      end="", flush=True)
            else:
                print("\033[2J\033[H◉ organum inspect --all — 활성 세포 대기 중… " + spin,
                      end="", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


def run(cwd: Path, state_dir: Path | None, transcript: str | None = None,
        once: bool = False, interval: float = 1.0, all_cells: bool = False) -> int:
    if all_cells:
        return _run_multi(cwd, state_dir, once=once, interval=interval)
    path = find_transcript(cwd, transcript)
    v = Vitals()
    if once:
        if path is None:
            print("organum inspect: 이 폴더의 활성 Claude Code 세션 transcript를 못 찾음.\n"
                  "  이 폴더에서 claude를 한 번 실행했는지 확인하거나 --transcript로 지정하세요.")
            return 1
        v.update(path)
        print(render(v, cwd, state_dir))
        return 0

    frames = "|/-\\"
    last_new = time.monotonic()
    i = 0
    try:
        while True:
            path = find_transcript(cwd, transcript)  # 세션이 새로 뜨면 따라잡음
            spin = frames[i % len(frames)]
            if path is not None:
                if v.update(path):
                    last_new = time.monotonic()
                idle = int(time.monotonic() - last_new)
                frame = render(v, cwd, state_dir, spinner=spin, idle=idle)
                print("\033[2J\033[H" + frame, end="", flush=True)
            else:
                print("\033[2J\033[H◉ organum inspect — 활성 세션 대기 중… " + spin + "\n"
                      f"  ({CLAUDE_PROJECTS / _mangle(cwd)} 에서 *.jsonl 을 찾는 중)",
                      end="", flush=True)
            i += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0
