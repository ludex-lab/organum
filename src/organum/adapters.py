"""organum adapters — 벤더별 세션 transcript 리더 (Claude·Codex·agy를 같은 관찰 수준으로).

코딩-에이전트 CLI는 세션을 append-only 로그로 남긴다(전부 per-event UTC timestamp). 어댑터가 벤더별
**발견(discover: cwd→세션 파일)**과 **파싱(read: 로그→정규화 Cell)**을 맡고, 관찰 소비자(roster·web·
inspect --all)는 등록된 어댑터 전부를 순회·병합한다. 정규화 Cell 덕에 "여러 브레인 하나의 유기체"가
벤더 무관하게 성립한다.

**경계: read-only 관찰만. organum이 세션을 spawn/route 하지 않는다.** stdlib only(json).
liveness는 파일 mtime이 아니라 마지막 *내용* timestamp(유령 세포 교훈).
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path

HOME = Path(os.path.expanduser("~"))


def _cell(vendor: str, sid: str, **kw) -> dict:
    """정규화된 관찰 Cell — 모든 어댑터가 이 모양으로 낸다.

    토큰(in_tok/out_tok/cache)의 **기본은 None=미측정**(벤더가 디스크에 안 남김 → 표시 '—').
    0은 '측정했고 0'이라는 다른 주장 — 잰 어댑터만 숫자를 채운다(표시 정직성)."""
    c = {
        "id": (sid or "?")[:8], "vendor": vendor, "session_id": sid,
        "model": None, "origin": "terminal", "parent": None, "in_tok": None, "out_tok": None,
        "cache": None,
        "tools": {}, "files": [], "branch": None, "skills": {}, "last_ts": None, "path": None,
        "first_ts": None,  # 세션 시작 ts — duration(사후 계측의 1급 축)의 원료. None=원천 부재
        "fallback": False,
    }
    c.update(kw)
    return c


def _pb_varint(b: bytes, i: int) -> tuple[int, int]:
    x = s = 0
    while True:
        if i >= len(b) or s > 63:
            raise ValueError("bad varint")
        c = b[i]; i += 1
        x |= (c & 0x7F) << s
        if not c & 0x80:
            return x, i
        s += 7


def _pb_fields(b: bytes) -> list:
    """protobuf 와이어 포맷 수동 디코드(stdlib) — (field, wire_type, value) 목록.
    스키마 없이 관측만 하므로 agy 버전이 필드를 더해도 깨지지 않는다(모르는 필드=무시)."""
    i, out = 0, []
    while i < len(b):
        tag, i = _pb_varint(b, i)
        f, wt = tag >> 3, tag & 7
        if f == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = _pb_varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            n, i = _pb_varint(b, i)
            v = b[i:i + n]; i += n
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        if wt in (1, 2, 5) and i > len(b):
            raise ValueError("truncated")
        out.append((f, wt, v))
    return out


class Adapter:
    """벤더 어댑터 인터페이스. discover(cwd)→세션 참조들, read(ref)→정규화 Cell."""
    name = "?"

    def available(self) -> bool:
        return False

    def discover(self, cwd, window_min: float = 30.0) -> list:
        return []

    def read(self, ref, deep: bool = False) -> dict | None:
        """deep=True: 정밀 사후 계측 모드 — 대용량 절삭 없이 전량 파싱 (라이브 폴링엔 False)."""
        return None


class ClaudeAdapter(Adapter):
    """Claude Code — `~/.claude/projects/<mangle(cwd)>/*.jsonl` + 세션별
    `<uuid>/subagents/agent-*.jsonl`(in-session 서브에이전트). 기존 inspect 로직 재사용."""
    name = "claude"

    def available(self) -> bool:
        return (HOME / ".claude" / "projects").is_dir()

    def discover(self, cwd, window_min: float = 30.0) -> list:
        from organum import inspect as ins
        return ins.find_all_transcripts(Path(cwd), window_min=window_min)

    def read(self, ref, deep: bool = False) -> dict | None:
        from organum import inspect as ins
        path = Path(ref)
        v = ins.Vitals()
        v.update(path)
        # 경로가 곧 계보: <부모세션uuid>/subagents/agent-<id>.jsonl. entrypoint는 이 경우에도
        # 'cli'로 남아(실측) 판별 불가 — sdk-cli 휴리스틱은 SDK-spawn 톱레벨 세션용으로 유지.
        sub = path.parent.name == "subagents"
        sid = path.stem.removeprefix("agent-") if sub else path.stem
        return _cell("claude", sid, path=str(path),
                     model=v.model,
                     origin=("subagent" if sub or v.entrypoint == "sdk-cli" else "terminal"),
                     parent=(path.parent.parent.name[:8] if sub else None),
                     in_tok=v.in_tok, out_tok=v.out_tok, cache=v.cache_read,
                     tools=dict(v.tools), files=sorted(v.files), branch=v.branch,
                     skills=dict(v.skills), first_ts=v.first_ts, last_ts=v.last_ts,
                     fallback=v.fallback)


class CodexAdapter(Adapter):
    """OpenAI Codex CLI — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (JSONL, per-line UTC ts).

    발견이 뒤집힌다: cwd가 경로에 없고 line-1 `session_meta.payload.cwd`에 있음 → 날짜 dir(오늘/어제)만
    glob → line-1 cwd 필터. (실물 v0.144.1 스키마로 검증: git nested, token_count.info.total_token_usage.)
    """
    name = "codex"

    def __init__(self, root: Path | None = None):
        self.root = root or (HOME / ".codex" / "sessions")

    def available(self) -> bool:
        return self.root.is_dir()

    def _recent_date_dirs(self, window_min: float = 30.0):
        """관측 창을 덮는 날짜 폴더들. 라이브 관측(30분)이면 오늘/어제 2개로 끝나고,
        observatory 광역 스윕(45일)이면 그만큼 거슬러 올라간다 — 창과 무관한 오늘/어제
        하드코딩은 스윕에서 codex만 이력이 누락되는 버그였다(warren Round One 실사고)."""
        today = datetime.date.today()
        days = int(window_min / 1440) + 1  # +1 = 자정 경계 대비
        out = []
        for i in range(days + 1):
            d = today - datetime.timedelta(days=i)
            p = self.root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
            if p.is_dir():
                out.append(p)
        return out

    def discover(self, cwd, window_min: float = 30.0) -> list:
        want = str(Path(cwd))
        cutoff = time.time() - window_min * 60
        refs = []
        for dd in self._recent_date_dirs(window_min):
            for fp in dd.glob("rollout-*.jsonl"):
                try:
                    if fp.stat().st_mtime < cutoff:
                        continue
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        m = json.loads(fh.readline() or "{}").get("payload", {})
                except (OSError, json.JSONDecodeError):
                    continue
                if (m.get("cwd") or "") == want:
                    refs.append(fp)
        return refs

    def read(self, ref, deep: bool = False) -> dict | None:
        path = Path(ref)
        try:
            size = path.stat().st_size
            with open(path, encoding="utf-8", errors="replace") as fh:
                if size > 8_000_000 and not deep:  # 라이브 폴링: line-1 + 최근 tail만 (deep=전량)
                    lines = fh.read(65536).split("\n")
                    fh.seek(max(0, size - 2_000_000)); fh.readline()
                    lines += fh.read().split("\n")
                else:
                    lines = fh.read().split("\n")
        except OSError:
            return None
        meta = {}; model = None; first_ts = None; last_ts = None
        in_t = out_t = cache = None  # token_count 이벤트를 보기 전엔 미측정
        tools: Counter = Counter(); files = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("timestamp"):
                first_ts = first_ts or ev["timestamp"]  # 절삭 모드여도 head에 line-1이 있어 정확
                last_ts = ev["timestamp"]
            t = ev.get("type"); p = ev.get("payload") or {}
            if t == "session_meta" and not meta:
                meta = p
            elif t == "turn_context" and p.get("model"):
                model = p["model"]
            elif t == "event_msg":
                pt = p.get("type")
                if pt == "token_count":
                    ttu = (p.get("info") or {}).get("total_token_usage") or {}
                    in_t = ttu.get("input_tokens") or in_t
                    out_t = ttu.get("output_tokens") or out_t
                    cache = ttu.get("cached_input_tokens") or cache
                elif pt == "patch_apply_end":
                    for fpath in (p.get("changes") or {}):
                        files.add(fpath)
            elif t == "response_item" and p.get("type") in ("function_call", "custom_tool_call"):
                tools[p.get("name", "?")] += 1
        git = meta.get("git")
        branch = (git.get("branch") if isinstance(git, dict) else None) or meta.get("git_branch")
        # 메인/서브 휴리스틱: exec/codex_exec = 헤드리스(≈서브) · tui/vscode = 인터랙티브(터미널)
        headless = meta.get("source") == "exec" or meta.get("originator") == "codex_exec"
        sid = meta.get("session_id") or meta.get("id") or path.stem
        return _cell("codex", sid, path=str(path), model=model,
                     origin=("subagent" if headless else "terminal"),
                     in_tok=in_t, out_tok=out_t, cache=cache, tools=dict(tools),
                     files=sorted(files), branch=branch, first_ts=first_ts, last_ts=last_ts)


class AgyAdapter(Adapter):
    """Google Antigravity `agy` CLI — `~/.gemini/antigravity-cli/brain/<uuid>/…/transcript_full.jsonl`.

    Tier-1: JSON 트랜스크립트 tail(presence·tools·files·created_at liveness). **Tier-2**: model·토큰은
    사이드카 SQLite(`conversations/<uuid>.db`, brain uuid와 1:1 — 실측 501/501 매칭)의 gen_metadata
    protobuf blob에서 — 요청별 레코드의 `1.4.2`=입력 · `1.4.3`=출력(=1.4.9+1.4.10, 실측 전행 성립) ·
    `1.21`=모델 표시명(폴백 `1.19`=id). cache 분해는 온디스크에 없음 → None(미측정 '—').
    cwd가 경로에 없어(opaque UUID) → tool arg의 절대경로가 cwd 아래인지로 join(best-effort).
    (실물 agy 1.0.16 스키마로 검증.)
    """
    name = "agy"
    _PATH_KEYS = ("AbsolutePath", "TargetFile", "FilePath", "Path", "DirectoryPath", "Cwd")

    def __init__(self, root: Path | None = None):
        self.root = root or (HOME / ".gemini" / "antigravity-cli")

    def available(self) -> bool:
        return (self.root / "brain").is_dir()

    def _transcript(self, d: Path):
        logs = d / ".system_generated" / "logs"
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            p = logs / name
            if p.is_file():
                return p
        return None

    def _gen_stats(self, sid: str) -> tuple[str | None, int | None, int | None]:
        """Tier-2 — 사이드카 gen_metadata에서 (model, in_tok, out_tok). 어떤 실패도 조용히
        (None, None, None)=Tier-1 폴백: 토큰은 미측정('—')으로 남지 0으로 뭉개지지 않는다."""
        db = self.root / "conversations" / f"{sid}.db"
        if not db.is_file():
            return None, None, None
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx").fetchall()
            conn.close()
        except sqlite3.Error:
            return None, None, None
        model, in_t, out_t = None, None, None
        for (data,) in rows:
            try:
                body = next((v for f, wt, v in _pb_fields(data) if f == 1 and wt == 2), None)
                if body is None:
                    continue
                f1 = _pb_fields(body)
                names = [v for f, wt, v in f1 if f == 21 and wt == 2]
                ids = [v for f, wt, v in f1 if f == 19 and wt == 2]
                if names or ids:  # 마지막 레코드의 모델이 현재 모델 (codex turn_context와 같은 규약)
                    model = (names[-1] if names else ids[-1]).decode("utf-8", "replace")
                stats = next((v for f, wt, v in f1 if f == 4 and wt == 2), None)
                if stats is None:
                    continue
                d = {f: v for f, wt, v in _pb_fields(stats) if wt == 0}
                if 2 in d:
                    in_t = (in_t or 0) + d[2]
                if 3 in d:
                    out_t = (out_t or 0) + d[3]
            except (ValueError, IndexError):
                continue  # 깨진 blob 하나가 나머지 관측을 막지 않는다
        return model, in_t, out_t

    def _belongs(self, tp: Path, cwd: str) -> bool:
        # Tier-1 cwd join: tool arg 절대경로가 cwd를 포함하면 이 세션 = cwd (첫 400줄 사전검사)
        try:
            with open(tp, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 400:
                        break
                    if cwd in line:
                        return True
        except OSError:
            return False
        return False

    def discover(self, cwd, window_min: float = 30.0) -> list:
        want = str(Path(cwd))
        cutoff = time.time() - window_min * 60
        brain = self.root / "brain"
        if not brain.is_dir():
            return []
        refs = []
        for d in brain.iterdir():
            if not d.is_dir():
                continue
            tp = self._transcript(d)
            if not tp:
                continue
            try:
                if tp.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            if self._belongs(tp, want):
                refs.append(tp)
        return refs

    def read(self, ref, deep: bool = False) -> dict | None:
        tp = Path(ref)
        first_ts = None; last_ts = None
        tools: Counter = Counter(); files = set()
        try:
            with open(tp, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("created_at"):
                        first_ts = first_ts or ev["created_at"]
                        last_ts = ev["created_at"]
                    for tc in ev.get("tool_calls") or []:
                        tools[tc.get("name", "?")] += 1
                        args = tc.get("args") or {}
                        for k in self._PATH_KEYS:
                            v = args.get(k)
                            if isinstance(v, str) and v.startswith("/"):
                                files.add(v)
        except OSError:
            return None
        try:
            sid = tp.parents[2].name  # brain/<uuid>/.system_generated/logs/… → parents[2]=uuid
        except IndexError:
            sid = tp.stem
        # Tier-2: 사이드카 protobuf에서 model·in/out (실패 시 None=미측정 '—'). origin=terminal 기본
        model, in_t, out_t = self._gen_stats(sid)
        return _cell("agy", sid, path=str(tp), model=model, origin="terminal",
                     in_tok=in_t, out_tok=out_t,
                     tools=dict(tools), files=sorted(files), first_ts=first_ts, last_ts=last_ts)


class GrokAdapter(Adapter):
    """xAI Grok CLI — `~/.grok/sessions/<url-encoded-cwd>/<session-id>/`.

    4벤더 중 제일 깨끗: 메타가 구조화 JSON에 다 있다 — summary.json(모델·cwd·branch·timestamps·
    agent_name·parent) + signals.json(토큰·tool 카운터). Tier-2(토큰)까지 두 JSON으로, 스캔/protobuf
    불필요. files는 updates.jsonl(ACP 이벤트)의 update.locations[].path. cwd 그룹 dir = urllib quote
    (슬래시가 %2F). (실물 Grok 0.2.99 스키마로 검증.)
    """
    name = "grok"

    def __init__(self, root: Path | None = None):
        self.root = root or (HOME / ".grok" / "sessions")

    def available(self) -> bool:
        return self.root.is_dir()

    def discover(self, cwd, window_min: float = 30.0) -> list:
        from urllib.parse import quote
        grp = self.root / quote(str(Path(cwd)), safe="")  # cwd가 그룹 dir 이름(url-encode)
        if not grp.is_dir():
            return []
        cutoff = time.time() - window_min * 60
        refs = []
        for sess in grp.iterdir():
            if not sess.is_dir():
                continue
            try:
                if (sess / "summary.json").stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            refs.append(sess)
        return refs

    def read(self, ref, deep: bool = False) -> dict | None:
        sess = Path(ref)
        try:
            summary = json.loads((sess / "summary.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        sid = (summary.get("info") or {}).get("id") or sess.name
        # signals.json의 contextTokensUsed = Grok의 유일한 온디스크 토큰 지표(≈입력).
        # out/cache 분해는 Grok CLI가 디스크에 안 남김(원천 부재) → None(미측정, '—')으로 정직하게.
        tokens = None
        try:
            sig = json.loads((sess / "signals.json").read_text(encoding="utf-8"))
            tokens = sig.get("contextTokensUsed")
        except (OSError, json.JSONDecodeError):
            pass
        # updates.jsonl: ACP 이벤트에서 tool 카운트(초기 tool_call만) + 파일(locations/target_file)
        tools: Counter = Counter(); files = set()
        try:
            with open(sess / "updates.jsonl", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        u = ((json.loads(line).get("params") or {}).get("update")) or {}
                    except json.JSONDecodeError:
                        continue
                    su = u.get("sessionUpdate")
                    if su == "tool_call":
                        tools[u.get("title") or "?"] += 1
                    if su in ("tool_call", "tool_call_update"):
                        for loc in u.get("locations") or []:
                            if isinstance(loc.get("path"), str):
                                files.add(loc["path"])
                        tf = (u.get("rawInput") or {}).get("target_file")
                        if isinstance(tf, str):
                            files.add(tf)
        except OSError:
            pass
        # origin: parent_session_id면 fork/subagent(비-primary) → subagent, 아니면 terminal
        origin = "subagent" if summary.get("parent_session_id") else "terminal"
        return _cell("grok", sid, path=str(sess), model=summary.get("current_model_id"),
                     origin=origin, parent=(summary.get("parent_session_id") or "")[:8] or None,
                     in_tok=tokens, tools=dict(tools), files=sorted(files),
                     branch=summary.get("head_branch"), first_ts=summary.get("created_at"),
                     last_ts=summary.get("last_active_at") or summary.get("updated_at"))


def _ms_to_iso(ms) -> str | None:
    """OpenCode의 epoch-밀리초 → ISO UTC (ts_age_seconds가 파싱하도록)."""
    if not ms:
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError, OverflowError):
        return None


_OC_ROOT_DOMAIN = b"organum-code/opencode-root/v1"


def opencode_cell_identity(root_session_id: str | None) -> str | None:
    """OpenCode root 세션 id → canonical organum cell id (organum-code identity 계약 v1).

    `oc-` + sha256(domain · NUL · root)[:36]. 하네스(organum-code `src/organum-identity.ts`
    `deriveCellIdentity`)와 **바이트 동일** 파생이라, 관측 어댑터가 raw `ses_` 세션을 하네스가
    join에 쓴 canonical cell로 귀속할 수 있다(two-lens·observatory가 exact-key로 조인). domain
    separator 없이는 재현 불가 — 그게 measurement replay에서 두-렌즈가 갈린 원인이었다.
    root가 비면 None(fail-closed = 미귀속 유지, 가짜 id 안 만듦)."""
    if not root_session_id:
        return None
    import hashlib
    digest = hashlib.sha256(_OC_ROOT_DOMAIN + b"\x00" + root_session_id.encode("utf-8")).hexdigest()
    return "oc-" + digest[:36]


def _opencode_root(conn, sid: str, directory: str, max_depth: int = 64) -> str | None:
    """parent_id 체인을 **같은 directory 안에서** 따라 root 세션 id를 구한다 (organum-code
    `resolveRootSession` 계약 포팅: directory-scoped·maxDepth 64). root = parent_id IS NULL인
    세션. cycle·depth 초과·missing lookup·contract 위반(id 불일치·빈/비-str parent)은 전부
    **None(fail-closed)** — 호출자는 raw id로 degrade해 정직하게 미귀속으로 남긴다."""
    if not sid or not directory:
        return None
    seen: set[str] = set()
    current = sid
    for _ in range(max_depth):
        if current in seen:                     # cycle
            return None
        seen.add(current)
        try:
            row = conn.execute(
                "SELECT id, parent_id FROM session WHERE id=? AND directory=?",
                (current, directory)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:                         # missing / lookup 실패
            return None
        if row["id"] != current:                # contract: 다른 id 반환
            return None
        parent = row["parent_id"]
        if parent is None:                      # parentID 부재 → root 도달
            return current
        if not isinstance(parent, str) or not parent:  # contract: 빈/비-str parent
            return None
        current = parent
    return None                                 # depth 초과


class OpenCodeAdapter(Adapter):
    """OpenCode (sst/opencode) — provider-agnostic 터미널 에이전트. OpenAI-호환이라 **API 모델
    (SolarOpen2 등)을 세포로 만드는 어답트 대상**이고, 한 하네스가 여러 모델을 태우므로
    관측이 harness=opencode × model=실제모델(예: solar-open2)로 뜬다.

    저장 = SQLite (OPENCODE_DATA_DIR 기본 ~/.local/share/opencode / `opencode.db`, WAL). `session`
    테이블에 directory·model(JSON)·tokens_*·time_updated(epoch ms)가 직접 컬럼 → Tier-2 토큰 완비.
    tools/files는 `part.data`(JSON)의 type=tool에서. (실물 opencode.db 스키마로 검증.)
    """
    name = "opencode"

    def __init__(self, root: Path | None = None):
        env = os.environ.get("OPENCODE_DATA_DIR")
        self.root = root or (Path(env) if env else HOME / ".local" / "share" / "opencode")

    def _db(self) -> str:
        return f"file:{self.root / 'opencode.db'}?mode=ro"

    def available(self) -> bool:
        return (self.root / "opencode.db").is_file()

    def discover(self, cwd, window_min: float = 30.0) -> list:
        want = str(Path(cwd))
        cutoff_ms = (time.time() - window_min * 60) * 1000
        try:
            conn = sqlite3.connect(self._db(), uri=True)
            rows = conn.execute(
                "SELECT id FROM session WHERE directory=? AND time_updated>=? AND time_archived IS NULL",
                (want, cutoff_ms)).fetchall()
            conn.close()
        except sqlite3.Error:
            return []
        return [r[0] for r in rows]

    def read(self, ref, deep: bool = False) -> dict | None:
        sid = str(ref)
        try:
            conn = sqlite3.connect(self._db(), uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
            if row is None:
                conn.close()
                return None
            parts = conn.execute("SELECT data FROM part WHERE session_id=?", (sid,)).fetchall()
            directory = row["directory"] if "directory" in row.keys() else ""
            root = _opencode_root(conn, sid, directory or "")
            conn.close()
        except sqlite3.Error:
            return None
        d = dict(row)
        model = d.get("model")
        try:  # model은 JSON blob {"id","providerID","variant"}
            mj = json.loads(model) if model else None
            if isinstance(mj, dict):
                model = mj.get("id") or model
        except (json.JSONDecodeError, TypeError):
            pass
        tools: Counter = Counter(); files = set()
        for (pdata,) in parts:
            try:
                p = json.loads(pdata)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(p, dict) and p.get("type") == "tool":
                tools[p.get("tool") or "?"] += 1
                inp = ((p.get("state") or {}).get("input")) or {}
                for k in ("filePath", "path", "file"):
                    v = inp.get(k)
                    if isinstance(v, str) and v.startswith("/"):
                        files.add(v)
        # id = root에서 파생한 canonical cell(하네스 계약) → declared presence·ledger와 exact-key
        # 조인. root 미해석(broken chain 등)이면 raw sid[:8]로 fail-closed(정직한 미귀속).
        return _cell("opencode", sid, id=(opencode_cell_identity(root) or (sid or "?")[:8]),
                     path=str(self.root / "opencode.db"), model=model,
                     origin=("subagent" if d.get("parent_id") else "terminal"),
                     parent=(d.get("parent_id") or "")[:8] or None,
                     in_tok=d.get("tokens_input"), out_tok=d.get("tokens_output"),
                     cache=d.get("tokens_cache_read"), tools=dict(tools),
                     files=sorted(files), first_ts=_ms_to_iso(d.get("time_created")),
                     last_ts=_ms_to_iso(d.get("time_updated")))


ADAPTERS: list = [ClaudeAdapter(), CodexAdapter(), AgyAdapter(), GrokAdapter(), OpenCodeAdapter()]


def snapshot(cwd, window_min: float = 30.0, adapters: list | None = None,
             deep: bool = False) -> list:
    """등록된 어댑터 전부에서 cwd의 활성 세션을 발견·파싱 → 정규화 Cell 리스트.
    한 벤더의 실패가 전체 관찰을 막지 않도록 어댑터별로 격리한다(read-only).
    deep=True: 사후 계측용 전량 파싱(대용량 절삭 없음) — 라이브 폴링엔 쓰지 말 것."""
    cells = []
    for ad in (ADAPTERS if adapters is None else adapters):
        try:
            if not ad.available():
                continue
            for ref in ad.discover(cwd, window_min=window_min):
                try:
                    c = ad.read(ref, deep=deep)
                except Exception:
                    continue
                if c:
                    cells.append(c)
        except Exception:
            continue
    return cells
