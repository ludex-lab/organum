"""organum observatory — 관측 영속화 (docs/observatory-design.md).

벤더 transcript는 ~30일 시한부(실측: 청소 주기 밖 증거 부재) — 정규화 Cell의
요약 레코드를 `.organum/observatory/<YYYY-MM>.jsonl` 월 샤드에 append하여 세션
소비 통계에 시간축을 준다. 데몬 없음: checkup·web·`observatory sync`가 같은
멱등 record()에 편승한다.

규율 상속: read-only 관측의 영속화(벤더 파일 불변) · None=미측정 그대로(C2) ·
불완전 관측(last_ts 없음)은 기록하지 않음(guard — 오염 방지) · raw transcript
비보관(집계 가능한 요약만).
"""

from __future__ import annotations

import contextlib as _contextlib
import datetime
import json
from pathlib import Path

DIR_NAME = "observatory"
# 세션 샤드만 매칭(`YYYY-MM.jsonl`) — 같은 디렉터리의 `integrity.jsonl`(core-integrity 시간축
# 로그)을 세션 로더가 유령 세션(vendor/session_id=None)으로 오독하던 버그 차단. session_id 가드와
# 이중.
_SHARD_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl"

# 프로세스-로컬 캐시: (state_dir, vendor, session_id) → 마지막 기록 last_ts.
# web 폴링이 후보 0일 때 디스크를 안 건드리게 하는 1차 게이트 — 진실은 샤드.
_recorded: dict[tuple, str] = {}


def _dir(state_dir: Path) -> Path:
    return state_dir / DIR_NAME


def _shard(state_dir: Path, last_ts: str) -> Path:
    return _dir(state_dir) / f"{last_ts[:7]}.jsonl"


def _shard_index(state_dir: Path) -> dict:
    """전체 샤드에서 (vendor, session_id) → 최신 last_ts. 멱등성의 진실 소스."""
    idx: dict = {}
    d = _dir(state_dir)
    if not d.is_dir():
        return idx
    for p in sorted(d.glob(_SHARD_GLOB)):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not r.get("session_id"):  # 비-세션 레코드(integrity 등) 방어
                        continue
                    k = (r.get("vendor"), r.get("session_id"))
                    ts = r.get("last_ts") or ""
                    if ts > (idx.get(k) or ""):
                        idx[k] = ts
        except OSError:
            continue
    return idx


# 전역 공유 저장소라 세션-스코프 스캔 없이 마커를 뽑으면 다른 세션 마커로 오조인된다
# (critic ②: opencode.db는 모든 세션 공유). 즉시 안전 = 마커 조인 끔(직접 id만). 세션-스코프
# 추출은 후속(adapter가 part WHERE session_id=?로).
_GLOBAL_STORE_VENDORS = {"opencode"}


def _role_of_cell(cands: list) -> dict:
    """선언 셀의 role-only fail-closed 추론. 반환 {role, intent, sid} (미정은 None).

    조인 키 = **모든 후보 세션이 non-empty role을 갖고 그 role이 유일**할 때만 role(critic 2:
    role 없는 세션이 섞여도 engine으로 조인하던 것 차단). 시간창 매칭 안 씀 — organum 세션의
    started_at/ended_at은 *선언 시각*이지 작업 창이 아니라(warren 실측 start→end 5초) brain 창
    포함 판정이 성립하지 않는다(critic ①③의 근거는 스키마 추론이라 이 데이터 사실을 몰랐음).
    role 회전은 end+start를 요구하므로 서로 다른 role이 둘↑이면 role을 가로질렀다 → None.
    **intent/sid_declared는 후보가 정확히 1개일 때만** 채운다(role 유일이 intent/session 조인으로
    조용히 승격하던 것 차단, critic 2)."""
    roles = [s.get("role") for s in cands]
    if not cands or not all(roles) or len(set(roles)) != 1:
        return {"role": None, "intent": None, "sid": None, "loadout": None, "problem_type": None}
    role = roles[0]
    if len(cands) == 1:  # 유일 후보만 intent/sid/loadout/problem_type 확정 — 여럿이면 대표 임의선택 금지
        return {"role": role, "intent": cands[0].get("intent"), "sid": cands[0].get("sid"),
                "loadout": cands[0].get("loadout"),
                "problem_type": cands[0].get("problem_type")}
    return {"role": role, "intent": None, "sid": None, "loadout": None, "problem_type": None}


def _declared_join(state_dir: Path, cells: list) -> dict:
    """관찰 셀 ↔ 선언 세션 fail-closed 조인 (협업벤치 v0.1). 조인 키 = 선언 셀 identity(직접 id
    또는 exact-unique 마커) + **셀당 role 유일성**(role-only inference — 세션 창은 선언 시각이지
    작업 창이 아니라 시간매칭 불가). 애매는 role=None. **모든 셀에 대해** provenance를 낸다:
    join_method(direct|marker|None) + join_status(joined|role-ambiguous|no-marker|marker-unknown|
    marker-ambiguous|scan-incomplete|global-store-disabled|no-bridge) — 미조인 이유 감사(critic)."""
    try:
        from organum import session as _sess
        from organum import web as _web
        sessions = _sess.sessions_for_join(state_dir)
    except Exception:
        sessions = []
    from organum.state import cell_key
    # session identity dict를 cell_key로 구성 — case-insensitive 계약(재감사4): Agent/agent가
    # 마지막 lookup만 정규화되고 grouping은 raw면 by_cell에서 갈린다. 여기서 통일.
    by_cell: dict = {}
    for s in sessions:
        if s.get("cell"):
            by_cell.setdefault(cell_key(s["cell"]), []).append(s)
    cids = list(by_cell)   # cell_key 형 (marker lookup도 cell_key로 매칭)
    out = {}
    for c in cells:
        method, declared, reason = None, None, "no-bridge"
        ckid = cell_key(c["id"])
        if ckid in by_cell:
            declared, method, reason = ckid, "direct", "found"
        elif not c.get("path"):
            reason = "no-bridge"
        elif c.get("vendor") in _GLOBAL_STORE_VENDORS:
            reason = "global-store-disabled"   # 전역 공유 DB(opencode) 마커 스캔 끔
        else:
            try:
                declared, reason = _web._find_declared(c["path"], cids)  # 구조화 원인
            except Exception:
                declared, reason = None, "scan-incomplete"
            if declared:
                method = "marker"
        cand = by_cell.get(declared) or []
        j = _role_of_cell(cand)
        if declared and j["role"]:
            status = "joined"
        elif declared:
            status = "role-ambiguous"          # identity 확인, role이 유일하지 않음
        else:
            status = reason
        out[c["id"]] = {
            # identity(declared·join_method)는 확정됐으면 role이 애매해도 **보존**(critic 3)
            "declared": declared, "role": j["role"],
            "intent": j["intent"], "sid_declared": j["sid"],
            "loadout": j.get("loadout"),   # organ 집합(v0.1.1 §1) — 미조인/애매는 None
            "problem_type": j.get("problem_type"),  # §5 taxonomy — 미조인/애매/미선언은 None
            "join_method": method, "join_status": status,
            "n_sessions": len(cand) if declared else 0,  # 조인 근거 세션 수
        }
    return out


_ATTR_KEYS = ("declared", "role", "intent", "sid_declared", "loadout", "problem_type",
              "join_method", "join_status")


def _attribution_changed(cur: dict, j: dict) -> bool:
    """persisted 레코드 cur의 attribution이 새로 계산한 조인 j와 다른가 — refresh가 교정 레코드를
    append할지 결정(멱등: 무변이면 no-op). 측정 필드(토큰·last_ts)는 비교 대상 아님 — refresh는
    attribution만 갱신한다."""
    for k in _ATTR_KEYS:
        if cur.get(k) != j.get(k):
            return True
    return (cur.get("declared_sessions") or 0) != j.get("n_sessions", 0)


def record(state_dir: Path, cells: list, reason: str,
           only_idle_sec: float | None = None, now: float | None = None,
           refresh: bool = False) -> int:
    """정규화 Cell들을 월 샤드에 멱등 append. 기록한 수를 반환.

    only_idle_sec: 마지막 내용 ts가 그보다 최근인 셀은 건너뜀 — web 폴링이
    활동 중 셀을 매 폴마다 찍지 않고 settle된 상태만 남기게(최종본은 checkup/sync가 보증).

    refresh: 같은 last_ts로 이미 기록된 세션도 **attribution만 재계산해 교정 레코드를 append**
    (append-only 유지, 로더가 tie에서 최신을 선호). attribution 로직이 나아지거나(어댑터 파생·
    declared-join) 선언 세션이 뒤늦게 나타나 이미 영속된 귀속이 stale일 때 자가교정 경로 —
    실제 변경 시만 append(멱등, bloat 없음). last_ts가 뒤로 간 stale 관측은 refresh여도 무기록.
    """
    from organum.inspect import ts_age_seconds
    from organum.state import utc_now_iso

    if not state_dir.is_dir():
        return 0
    candidates = []
    for c in cells:
        if not c.get("last_ts"):  # 불완전 관측 미기록 (guard)
            continue
        if only_idle_sec is not None:
            age = ts_age_seconds(c["last_ts"], now)
            if age is None or age < only_idle_sec:
                continue
        key = (str(state_dir), c["vendor"], c.get("session_id"))
        if not refresh and _recorded.get(key) == c["last_ts"]:  # 1차 게이트: 프로세스 캐시(refresh는 재평가)
            continue
        candidates.append((key, c))
    if not candidates:
        return 0
    idx = _shard_index(state_dir)  # 2차 게이트: 샤드 진실
    joins = _declared_join(state_dir, [c for _, c in candidates])
    cur_recs = ({(r.get("vendor"), r.get("session_id")): r for r in load(state_dir)}
                if refresh else {})  # refresh: 같은 last_ts 재귀속 시 현재 attribution과 대조(멱등)
    n = 0
    for key, c in candidates:
        prev = idx.get((c["vendor"], c.get("session_id"))) or ""
        if c["last_ts"] < prev:  # stale 관측 — 전진분만 기록(refresh도 예외 아님)
            _recorded[key] = prev
            continue
        j = joins.get(c["id"]) or {}
        if c["last_ts"] == prev and not (refresh and _attribution_changed(
                cur_recs.get((c["vendor"], c.get("session_id"))) or {}, j)):
            # 이미 이 last_ts로 기록됨 + (refresh 아님 or attribution 무변) → skip(멱등)
            _recorded[key] = prev
            continue
        rec = {
            "v": 1, "vendor": c["vendor"], "session_id": c.get("session_id"),
            "id": c["id"], "model": c["model"], "origin": c["origin"],
            "parent": c.get("parent"),
            "in_tok": c["in_tok"], "out_tok": c["out_tok"], "cache": c["cache"],
            "tools": c["tools"] or {}, "skills": c["skills"] or {},
            "files_touched": len(c["files"] or []), "branch": c["branch"],
            "first_ts": c.get("first_ts"), "last_ts": c["last_ts"],
            "declared": j.get("declared"), "role": j.get("role"),
            "intent": j.get("intent"), "sid_declared": j.get("sid_declared"),
            "loadout": j.get("loadout"),   # organ 효과 매트릭스 v0.1.1 §1 (미조인=None)
            "problem_type": j.get("problem_type"),  # §5 문제 축 (미조인/미선언=None)
            # §6: worker 세팅 row는 role이 problem_type을 함의(교란) — 집계기가 둘의 효과를
            # 분리 주장할 수 없음을 주석이 아니라 필드로 강제. 미조인 row는 None(주장 자체 불가).
            "joint_observed": True if j.get("role") else None,
            # row provenance(critic): role_basis 고정 + method/status로 5/17 미조인 이유 감사
            "role_basis": "cell-role-unique", "join_method": j.get("join_method"),
            "join_status": j.get("join_status"), "declared_sessions": j.get("n_sessions", 0),
            "captured_at": utc_now_iso(), "capture_reason": reason,
        }
        shard = _shard(state_dir, c["last_ts"])
        shard.parent.mkdir(parents=True, exist_ok=True)
        with open(shard, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _recorded[key] = c["last_ts"]
        n += 1
    return n


# ── core-integrity 시간축 감시 (memory-surveillance observatory tier) ─────────
# checkup의 시점 검사와 달리, core 산출물의 blessed/unblessed 이력을 **transition 로그**(변화 시에만
# append)에 쌓아 "닷새간 unblessed로 떠 있다"(에세이의 fossil = 방치된 append가 sediment화)를 시간축에서
# 잡는다. 설계: docs/memory-surveillance-v0.md §5. 정직 경계: 탐지지 판결 아님(git-기반, 귀속 부분적).
FOSSIL_DAYS = 5.0


def _integrity_log(state_dir: Path) -> Path:
    return _dir(state_dir) / "integrity.jsonl"


def _writable_state(state_dir: Path) -> bool:
    """초기화된 meta + **정확 지원 format**일 때만 (critic B4: future-format·반쪽 init에 감시 로그 쓰기 금지)."""
    from organum import FORMAT_VERSION
    p = state_dir / "meta.json"
    if not p.is_file():
        return False
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(m, dict) and m.get("format_version") == FORMAT_VERSION


def _read_integrity_log(state_dir: Path):
    """(records, incomplete). **append 순서** dict record + typed 필드(path str·status str·context는
    list-of-dict)만 채택. scalar/list/wrong-type 줄·decode/read 실패는 scan을 죽이지 않고 skip하되
    **incomplete=True로 표면화**(critic B5: 손상을 clean/이력없음으로 조용히 바꾸지 않음)."""
    log = _integrity_log(state_dir)
    if not log.is_file():
        return [], False
    try:
        raw = log.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], True   # decode/read 실패 = incomplete
    from organum import integrity as _integ
    recs, incomplete = [], False
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            incomplete = True
            continue
        if not (isinstance(r, dict) and isinstance(r.get("path"), str) and r["path"]
                and isinstance(r.get("status"), str) and r["status"] in _integ.VALID_STATUS):
            incomplete = True   # 미지 status("blesssed" 오타 등)도 drop + 표면화(critic B5-b)
            continue
        ctx = r.get("context_at_observation", r.get("context"))
        if ctx is not None and not (isinstance(ctx, list) and all(isinstance(x, dict) for x in ctx)):
            incomplete = True   # context가 list-of-dict 아니면 드롭(CLI c.get() crash 방지)
            continue
        recs.append(r)
    return recs, incomplete


def integrity_incomplete(state_dir: Path) -> bool:
    """감시 로그가 손상돼 last-observed/current를 '완전'하다고 주장할 수 없나(critic B5)."""
    return _read_integrity_log(state_dir)[1]


def _integrity_last(state_dir: Path) -> dict:
    """path → **append 순서상 마지막** (status, rev). transition 판정용(ts 아님 — critic B2 ordering)."""
    last: dict[str, tuple] = {}
    for r in _read_integrity_log(state_dir)[0]:
        last[r["path"]] = (r.get("status"), r.get("rev", ""))
    return last


def record_integrity(state_dir: Path, project_root: Path | None = None) -> int:
    """core-integrity 스냅샷을 transition 로그에 append — **상태(status, rev) 변화 시에만**(로그 안 커짐).
    web·observatory sync가 편승. **초기화 meta + 정확 format일 때만 쓴다**(critic B4). git 아니면 0.
    context는 **관측 시점(sweep) 활성 세션** — 변이 시점 아님(critic B3, forensic evidence)."""
    from organum import integrity as _integ
    from organum import session as _sess
    if not state_dir.is_dir() or not _writable_state(state_dir):
        return 0
    project = project_root or state_dir.parent
    if not _integ.is_git_repo(project):
        return 0
    last = _integrity_last(state_dir)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    context = [{"cell": s.get("cell"), "role": s.get("role")}
               for s in _sess.open_sessions(state_dir)]
    log = _integrity_log(state_dir)
    n = 0
    for r in _integ.report(state_dir):
        rev = (r.get("last_commit") or {}).get("rev") or ""
        if last.get(r["path"]) == (r["status"], rev):
            continue  # 변화 없음 — transition 아님
        rec = {"ts": now, "path": r["path"], "status": r["status"], "rev": rev,
               "authority": r.get("authority", ""),
               "context_at_observation": context}   # B3: 관측(sweep) 시점 — 변이 시점 아님
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
    return n


def _drift_days(since: str, now: datetime.datetime):
    """(days, unknown). invalid/future ts → (None, True) — fossil을 조용히 false로 단정 안 함(critic B2)."""
    try:
        t = datetime.datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return None, True
    d = (now - t).total_seconds() / 86400
    return (None, True) if d < 0 else (d, False)


def integrity_drift(state_dir: Path, fossil_days: float = FOSSIL_DAYS) -> list:
    """core 항목별 **last-observed** 상태 + **episode 시작**(연속 non-blessed의 시작 = fossil age).
    critic B2 수리: current는 **append 순서**의 마지막(ts max 아님) · fossil age는 provenance rev 변경이
    아니라 **blessed→non-blessed 경계**부터(rev flapping이 age 리셋 못 함) · future/invalid ts는
    age_unknown(조용히 fossil false 안 함). live가 아니라 last-observed 로그 기반(정직)."""
    recs_by_path: dict[str, list] = {}
    for r in _read_integrity_log(state_dir)[0]:  # append 순서 보존(records만)
        recs_by_path.setdefault(r["path"], []).append(r)
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for path in sorted(recs_by_path):
        seq = recs_by_path[path]
        cur = seq[-1]                          # append 순서 마지막 = last-observed (ts 정렬 아님)
        status = cur.get("status")
        since_rec = cur
        if status != "blessed":                # 연속 non-blessed episode의 시작(경계)을 since로
            for r in reversed(seq):
                if r.get("status") == "blessed":
                    break
                since_rec = r
        since = since_rec.get("ts")
        drift, unknown = _drift_days(since, now)
        ctx = cur.get("context_at_observation")
        if ctx is None:
            ctx = cur.get("context") or []     # 옛 로그 하위호환
        out.append({
            "path": path, "status": status, "since": since,
            "drift_days": round(drift, 1) if drift is not None else None,
            "age_unknown": unknown,
            "fossil": bool(status != "blessed" and drift is not None and drift >= fossil_days),
            # B3: '관측 시점' 활성 세션(변이 시점 아님) — forensic evidence, verdict 아님
            "context_at_observation": ctx,
            "no_context_at_observation": bool(status != "blessed" and not ctx),
        })
    return out


def load(state_dir: Path, since_days: float | None = None) -> list:
    """샤드 전체 → (vendor, session_id)별 최신 last_ts 레코드만(라스트-라이트-윈),
    since_days 안의 것만. last_ts 오름차순."""
    latest: dict = {}
    d = _dir(state_dir)
    if not d.is_dir():
        return []
    for p in sorted(d.glob(_SHARD_GLOB)):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not r.get("session_id"):  # 비-세션 레코드(integrity 등) 방어
                        continue
                    k = (r.get("vendor"), r.get("session_id"))
                    prev = (latest.get(k) or {}).get("last_ts") or ""
                    if (r.get("last_ts") or "") >= prev:
                        latest[k] = r
        except OSError:
            continue
    recs = sorted(latest.values(), key=lambda r: r.get("last_ts") or "")
    if since_days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recs = [r for r in recs if (r.get("last_ts") or "") >= cutoff]
    return recs


def _sum_measured(recs: list, key: str) -> tuple[int | None, int]:
    """(측정된 것만의 합 또는 None, 미측정 수) — C2: None은 0으로 뭉개지 않는다."""
    vals = [r.get(key) for r in recs if r.get(key) is not None]
    return (sum(vals) if vals else None), len(recs) - len(vals)


def _cost(recs: list) -> float | None:
    """단가표(내장+사용자 ~/.organum/prices.json) 있는 모델만 합산. cache 쓰기 단가는
    원천 미보관 → 제외(근사). 단가는 사용자 제공(BYO) — 우리는 지어내지 않는다."""
    from organum.inspect import effective_prices
    prices = effective_prices()
    total, any_priced = 0.0, False
    for r in recs:
        pr = prices.get(r.get("model") or "")
        if not pr:
            continue
        any_priced = True
        total += ((r.get("in_tok") or 0) * pr["in"] + (r.get("out_tok") or 0) * pr["out"]
                  + (r.get("cache") or 0) * pr["cache_read"]) / 1_000_000
    return total if any_priced else None


def stats(recs: list, by: str | None = None) -> dict:
    """집계 — 세션 수·origin 분해·토큰(측정분만)·비용 근사·그룹별(by=model|role|origin|vendor)."""
    out: dict = {"sessions": len(recs),
                 "terminal": sum(1 for r in recs if r.get("origin") == "terminal"),
                 "subagent": sum(1 for r in recs if r.get("origin") == "subagent")}
    for k in ("in_tok", "out_tok", "cache"):
        out[k], out[f"{k}_unmeasured"] = _sum_measured(recs, k)
    out["cost_usd"] = _cost(recs)
    if by:
        groups: dict = {}
        for r in recs:
            g = str(r.get(by) or "—")
            groups.setdefault(g, []).append(r)
        out["by"] = {g: {"sessions": len(rs),
                         "in_tok": _sum_measured(rs, "in_tok")[0],
                         "out_tok": _sum_measured(rs, "out_tok")[0],
                         "cache": _sum_measured(rs, "cache")[0],
                         "cost_usd": _cost(rs)}
                     for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1]))}
    return out


def _fmt_tok(v) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _local_date(ts: str | None) -> str | None:
    """ISO UTC → 사람의 하루(로컬 날짜). 리포트의 '오늘'·일별 추이는 로컬 기준이 정직하다."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return None


SPARK = "▁▂▃▄▅▆▇█"


def _spark(vals: list) -> str:
    top = max((v for v in vals if v), default=0)
    if not top:
        return "▁" * len(vals)
    return "".join(SPARK[min(int((v or 0) / top * (len(SPARK) - 1) + 0.5), 7)] for v in vals)


def report(state_dir: Path, cwd: Path, days: float = 30) -> str:
    """작업 모니터 리포트 — '지금'(transcript 직독)과 '역사'(observatory 축적)를 분리된
    밴드로. 밴드끼리 합산하지 않는다(현재 세션은 settle 후 역사에도 편입 — 이중계산 방지).
    read-only: 리포트 생성이 기록을 남기지 않는다."""
    from organum import adapters as _ad
    from organum.inspect import ts_age_seconds

    lines = [f"━ organum report · {cwd.name} · 창 {days:g}일"]

    # ── 지금: 관측 창(30분) 안의 세션, 각 세션 생애 누적 (관제탑과 같은 눈)
    live = []
    for c in _ad.snapshot(cwd, window_min=30.0):
        age = ts_age_seconds(c.get("last_ts"))
        if age is not None and age <= 1800:
            live.append((c, age))
    lines.append(f"\n■ 지금 — 살아있는 세션 {len(live)} (관측 창 30분, 세션 생애 누적)")
    for c, age in sorted(live, key=lambda x: x[1]):
        state = "live" if age < 90 else f"idle {int(age // 60)}m"
        lines.append(f"  {c['id']}  {c['model'] or '—'} · {c['origin']}"
                     f" · out {_fmt_tok(c['out_tok'])} · cache {_fmt_tok(c['cache'])}"
                     f" · tools {sum((c['tools'] or {}).values())} · {state}")

    # ── 역사: observatory 축적 (transcript 청소를 넘어 살아남는 쪽)
    recs = load(state_dir, since_days=days)
    if not recs:
        lines.append("\n■ 역사 — 스냅샷 없음 (organum observatory sync 로 시작)")
        return "\n".join(lines)
    today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    today_recs = [r for r in recs if _local_date(r.get("last_ts")) == today]
    ts_ = stats(today_recs)
    lines.append(f"\n■ 오늘 — {ts_['sessions']} 세션 스냅샷"
                 f" · out {_fmt_tok(ts_['out_tok'])} · cache {_fmt_tok(ts_['cache'])}"
                 f" (서브에이전트 {ts_['subagent']})")

    s = stats(recs, by="model")
    lines.append(f"\n■ 역사 — {s['sessions']} 세션 · out {_fmt_tok(s['out_tok'])}"
                 f" · cache {_fmt_tok(s['cache'])}"
                 f" (터미널 {s['terminal']} · 서브에이전트 {s['subagent']})")
    if s.get("cost_usd") is not None:
        lines.append(f"  비용 근사: ${s['cost_usd']:.2f} (단가표 등재 모델만, 캐시 쓰기 제외)")
    # 일별 추이 (out 기준) — 대형 세션 지배 구조가 한눈에
    by_day: dict = {}
    for r in recs:
        d = _local_date(r.get("last_ts"))
        if d:
            by_day[d] = by_day.get(d, 0) + (r.get("out_tok") or 0)
    days_sorted = sorted(by_day)
    lines.append(f"  일별 out: {_spark([by_day[d] for d in days_sorted])}"
                 f"  ({days_sorted[0][5:]}~{days_sorted[-1][5:]}, 峰 {_fmt_tok(max(by_day.values()))})")
    # 모델 믹스 + 대형 세션 top3
    mix = " · ".join(f"{m} {g['sessions']}" for m, g in list(s["by"].items())[:5])
    lines.append(f"  모델 믹스: {mix}")
    top = sorted(recs, key=lambda r: r.get("out_tok") or 0, reverse=True)[:3]
    lines.append("  대형 세션:")
    for r in top:
        who = f" ({r['declared']} · {r['role']})" if r.get("role") else ""  # role 없으면 '—'(critic ③)
        lines.append(f"    {r['id']}{who}  {_local_date(r.get('last_ts'))}"
                     f" · out {_fmt_tok(r.get('out_tok'))} · cache {_fmt_tok(r.get('cache'))}"
                     f" · {r.get('model') or '—'}")
    return "\n".join(lines)


def render_stats(s: dict, days: float, by: str | None = None) -> str:
    lines = [f"observatory — 최근 {days:g}일 · {s['sessions']} 세션"
             f" (터미널 {s['terminal']} · 서브에이전트 {s['subagent']})"]
    unm = [f"{k.replace('_tok', '')} {n}셀" for k, n in
           ((x, s[f"{x}_unmeasured"]) for x in ("in_tok", "out_tok", "cache")) if n]
    lines.append(f"  토큰: in {_fmt_tok(s['in_tok'])} · out {_fmt_tok(s['out_tok'])}"
                 f" · cache {_fmt_tok(s['cache'])}"
                 + (f"  (미측정 제외: {', '.join(unm)})" if unm else ""))
    if s.get("cost_usd") is not None:
        lines.append(f"  비용 근사: ${s['cost_usd']:.2f} (단가표 등재 모델만, 캐시 쓰기 제외)")
    if by and s.get("by"):
        lines.append(f"  --by {by}:")
        w = max(len(g) for g in s["by"])
        for g, gs in s["by"].items():
            cost = f" · ${gs['cost_usd']:.2f}" if gs.get("cost_usd") is not None else ""
            lines.append(f"    {g:<{w}}  {gs['sessions']:>4}세션 · in {_fmt_tok(gs['in_tok'])}"
                         f" · out {_fmt_tok(gs['out_tok'])} · cache {_fmt_tok(gs['cache'])}{cost}")
    return "\n".join(lines)


# ── reported ingestion (organum-code observation/v1) ─────────────────────────
# ACP-라우팅 백엔드(grok-build·claude-code·deepcode …)의 세션은 supervisor-owned actor scope에
# 저장돼 organum의 수동 어댑터 관측에 불투명하다. per-backend 어댑터 포팅은 treadmill(OpenCode
# 하나 하드코딩했더니 보안 사유로 드롭됨) — 대신 organum-code supervisor가 emit하는 정규화
# observation/v1 레코드를 ingest한다. 계약: docs/observation-ingestion-contract-response.md
# (organum-code, Zod가 정본 — 여기 검증은 fail-closed 방어이지 스키마 재구현이 아님).
#
# 의미론 (organum-code 판정 요청 6건 그대로):
#  1. backend.id는 open — 문법 유효(비어있지 않은 소문자 슬러그)면 미지 백엔드도 수용.
#  2. backend는 vendor/model과 별개 1급 축 (provider≠harness 분리 = harness-lift 전제).
#  3. nullable identity·실패 run도 정상 observation (성공 행만 세면 정직하지 않다).
#  4. usage null 보존 — 미측정을 0으로 바꾸지 않는다 (C2).
#  5. (reported, run.id) 멱등 — 같은 payload 재전송=no-op, 다른 payload=conflict fail-closed.
#  6. passive/reported는 별도 evidence — exact nativeSessionId 일치 전까지 merge 금지.
OBS_V1_SCHEMA = "organum-code/observation/v1"
_REPORTED_GLOB = "reported-[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl"


class IngestConflict(ValueError):
    """같은 run.id에 다른 payload — silent overwrite 대신 fail-closed (계약 5)."""


def _obs_get(record: dict, *path):
    cur = record
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _parse_ts(value):
    """검증된 UTC `Z` RFC3339 문자열 → aware datetime, 아니면 None (critic A1).
    구조 검증(스키마 pattern이 Z-only 강제)을 통과한 값만 와야 하지만, 여기서도 Z-종결·UTC를
    이중 확인한다 — parse 실패/offset은 None(호출자가 fail-closed)."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.utcoffset() != datetime.timedelta(0):
        return None
    return dt


def validate_observation(record: dict) -> list[str]:
    """observation/v1 검증 — 위반 사유 목록 반환(빈 목록=통과). 2층(critic A2):

    ① **구조 = vendored portable schema 해석**(obs_schema — producer Zod에서 자동 생성된
      projection이 문법의 단일 정본; consumer 손-포팅 문법은 반드시 발산해서 폐지).
      required/type/enum/pattern/bounds/strict를 여기서 전부 강제.
    ② **cross-field = manual invariant**(projection에 없는 superRefine 등가) + **timestamp
      parse·ordering**(critic A1: `startedAt ≤ finishedAt ≤ recordedAt`, UTC `Z`만)."""
    if not isinstance(record, dict):
        return ["record가 object가 아님"]
    if record.get("schema") != OBS_V1_SCHEMA:
        return [f"schema != {OBS_V1_SCHEMA}"]      # 스키마 불일치면 이후 검증 무의미
    from organum import obs_schema
    errs = obs_schema.validate(record, obs_schema.load_observation_schema())
    if errs:
        return errs                                 # 구조가 깨졌으면 cross-field 무의미
    # ── cross-field invariants (superRefine 등가 — 구조 통과 후) ──
    # timestamp: 구조 검증이 UTC Z pattern을 이미 강제 — 여기서 parse해 ordering 확인 (A1)
    started = _parse_ts(_obs_get(record, "run", "startedAt"))
    finished = _parse_ts(_obs_get(record, "run", "finishedAt"))
    recorded = _parse_ts(_obs_get(record, "run", "recordedAt"))
    if recorded is None:
        errs.append("run.recordedAt이 UTC Z datetime으로 parse 불가")
    if _obs_get(record, "run", "startedAt") is not None and started is None:
        errs.append("run.startedAt parse 불가")
    if _obs_get(record, "run", "finishedAt") is not None and finished is None:
        errs.append("run.finishedAt parse 불가")
    if started and finished and started > finished:
        errs.append("startedAt > finishedAt (ordering 위반)")
    if finished and recorded and finished > recorded:
        errs.append("finishedAt > recordedAt (ordering 위반)")
    if started and recorded and started > recorded:
        errs.append("startedAt > recordedAt (ordering 위반)")
    if _obs_get(record, "run", "timingCompleteness") == "complete":
        if not (started and finished):
            errs.append("timingCompleteness=complete인데 start/finish 불완전")
    # identity
    cell = _obs_get(record, "identity", "canonicalCell")
    js = _obs_get(record, "identity", "joinStatus")
    if js == "joined" and not cell:
        errs.append("joinStatus=joined인데 canonicalCell 없음")
    if js == "not-joined" and cell is not None:
        errs.append("joinStatus=not-joined인데 canonicalCell 존재")
    persona = _obs_get(record, "identity", "persona")
    workspace = _obs_get(record, "identity", "workspace")
    if (persona is None) != (workspace is None):
        errs.append("persona/workspace는 함께 있거나 함께 null")
    # coordination (receipt basename 문법은 스키마 pattern이 강제 — "."·".."·구분자·NUL 거부)
    contrib = _obs_get(record, "coordination", "contributions")
    if isinstance(contrib, int) and contrib > 0:
        if not cell:
            errs.append("contribution>0인데 canonicalCell 없음")
        if not _obs_get(record, "coordination", "receipt", "file"):
            errs.append("contribution>0인데 receipt 없음")
    # outcome
    if (_obs_get(record, "outcome", "gate") == "pass"
            and _obs_get(record, "run", "status") != "passed"):
        errs.append("gate=pass인데 run.status != passed")
    if isinstance(contrib, int) and contrib == 0 and _obs_get(record, "coordination", "receipt") is not None:
        errs.append("contributions=0인데 receipt 존재 (null이어야)")
    # usage 관계 (전부 non-null일 때만 — null 보존이 우선)
    u = record.get("usage") or {}
    it, ot, tt = u.get("inputTokens"), u.get("outputTokens"), u.get("totalTokens")
    if it is not None and ot is not None and tt is not None and it + ot != tt:
        errs.append("totalTokens != inputTokens + outputTokens")
    if it is not None and u.get("cachedInputTokens") is not None and u["cachedInputTokens"] > it:
        errs.append("cachedInputTokens > inputTokens")
    if ot is not None and u.get("reasoningTokens") is not None and u["reasoningTokens"] > ot:
        errs.append("reasoningTokens > outputTokens")
    # usage completeness ↔ counter 정합 (critic A2.1 — producer superRefine 등가):
    # 라벨과 측정값이 서로 모순이면 "미측정을 0으로 안 쓴다" 규율 자체가 무너진다.
    comp = u.get("completeness")
    counters = ("responses", "inputTokens", "outputTokens", "cachedInputTokens",
                "totalTokens", "reasoningTokens")
    if comp == "unavailable":
        if u.get("source") != "unavailable":
            errs.append("completeness=unavailable인데 source가 unavailable 아님")
        for k in ("requests", "costUsd", *counters):
            if u.get(k) is not None:
                errs.append(f"completeness=unavailable인데 usage.{k} non-null")
    elif comp in ("complete", "lower-bound"):
        if u.get("source") == "unavailable":
            errs.append(f"completeness={comp}인데 source=unavailable")
        for k in counters:
            if u.get(k) is None:
                errs.append(f"completeness={comp}인데 usage.{k} null")
    if comp == "complete" and u.get("requests") is None:
        errs.append("completeness=complete인데 requests null")
    if (u.get("requests") is not None and u.get("responses") is not None
            and u["responses"] > u["requests"]):
        errs.append("responses > requests")
    if _obs_get(record, "provenance", "observationSource") != "reported":
        errs.append("provenance.observationSource != reported")
    return errs


def _payload_fp(record: dict) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _anchor_dt(record: dict):
    """샤드/시간 필터 앵커 datetime: finishedAt > startedAt > recordedAt. **parse된 신뢰값만**
    반환(critic A1: raw 문자열이 경로에 못 들어감 — shard basename은 여기서 strftime으로만)."""
    for k in ("finishedAt", "startedAt", "recordedAt"):
        dt = _parse_ts(_obs_get(record, "run", k))
        if dt is not None:
            return dt
    return None


@_contextlib.contextmanager
def _reported_lock(state_dir: Path):
    """cross-process critical section (critic A3) — index check+append를 한 락 안에.
    fcntl(macOS/Linux) 우선, msvcrt(Windows) 폴백. 락 파일은 관측 데이터가 아니라 조율 장치."""
    d = _dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / "reported.lock", "a+", encoding="utf-8")
    locked = None
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = "fcntl"
        except ImportError:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            locked = "msvcrt"
        yield
    finally:
        try:
            if locked == "fcntl":
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif locked == "msvcrt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()


def _reported_index(state_dir: Path) -> dict:
    """전체 reported 샤드에서 run_id → payload_fp (멱등/conflict의 진실 소스)."""
    idx: dict = {}
    d = _dir(state_dir)
    if not d.is_dir():
        return idx
    for p in sorted(d.glob(_REPORTED_GLOB)):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("run_id"):
                        idx[r["run_id"]] = r.get("payload_fp") or ""
        except OSError:
            continue
    return idx


def ingest_report(state_dir: Path, record: dict) -> str:
    """observation/v1 레코드 하나를 reported 샤드에 upsert. 반환: 'ingested'|'duplicate'.
    검증 실패=ValueError(사유 병기), 같은 run.id 다른 payload=IngestConflict. 원본 레코드
    전체를 보존한다(raw evidence 보존 — 평탄화는 load_reported가 읽기 시점에)."""
    errs = validate_observation(record)
    if errs:
        raise ValueError("observation/v1 검증 실패: " + " · ".join(errs[:5]))
    rid = record["run"]["id"]
    fp = _payload_fp(record)
    anchor = _anchor_dt(record)
    if anchor is None:                             # 검증 통과면 불가능하지만 이중 방어 (A1)
        raise ValueError("anchor timestamp parse 실패")
    from organum.state import utc_now_iso
    with _reported_lock(state_dir):                # index check+append 원자화 (A3)
        existing = _reported_index(state_dir)
        if rid in existing:
            if existing[rid] == fp:
                return "duplicate"                 # 멱등 재전송 (계약 5)
            raise IngestConflict(f"run.id {rid[:20]}…에 다른 payload — 거부(fail-closed)")
        # shard basename은 parse된 datetime의 strftime에서만 (A1 — raw 문자열 경로 금지)
        shard = _dir(state_dir) / f"reported-{anchor.strftime('%Y-%m')}.jsonl"
        row = {"v": 1, "run_id": rid, "payload_fp": fp, "ingested_at": utc_now_iso(),
               "record": record}
        with open(shard, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return "ingested"


def _reported_rows_and_conflicts(state_dir: Path) -> tuple[dict, set]:
    """reported 샤드 원시 스캔 → (run_id→row, conflict run_id 집합). 같은 run_id에 서로 다른
    payload_fp가 공존하면 conflict — **LWW로 하나를 고르지 않는다**(critic A3: 약속된
    conflict fail-closed가 silent overwrite로 바뀌는 것 방지, 격리)."""
    latest: dict = {}
    fps: dict = {}
    conflicts: set = set()
    d = _dir(state_dir)
    if not d.is_dir():
        return {}, set()
    for p in sorted(d.glob(_REPORTED_GLOB)):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rid = row.get("run_id")
                    if not rid or not isinstance(row.get("record"), dict):
                        continue
                    fp = row.get("payload_fp") or ""
                    if rid in fps and fps[rid] != fp:
                        conflicts.add(rid)         # 같은 run·다른 payload → 격리 (A3)
                    fps[rid] = fp
                    prev = latest.get(rid)
                    if prev is None or (row.get("ingested_at") or "") >= (prev.get("ingested_at") or ""):
                        latest[rid] = row
        except OSError:
            continue
    return latest, conflicts


def reported_conflicts(state_dir: Path) -> list[str]:
    """격리된 conflict run_id 목록 — CLI가 표면화(조용한 누락 방지)."""
    return sorted(_reported_rows_and_conflicts(state_dir)[1])


def load_reported(state_dir: Path, since_days: float | None = None) -> list:
    """reported 샤드 → stats 호환 평탄 행. null 보존(C2). conflict run은 **격리·제외**(A3 —
    reported_conflicts로 표면화). 행 필드: 기존 stats 축 + backend(1급, vendor와 별개) + source."""
    latest, conflicts = _reported_rows_and_conflicts(state_dir)
    out = []
    for rid, row in latest.items():
        if rid in conflicts:
            continue                               # 격리 — LWW 금지 (A3)
        r = row["record"]
        g = lambda *p: _obs_get(r, *p)  # noqa: E731
        u = r.get("usage") or {}
        out.append({
            "source": "reported", "run_id": row["run_id"], "ingested_at": row.get("ingested_at"),
            # 축: backend=하네스(1급), model/provider=brain — conflate 금지(harness-lift 전제)
            "backend": g("backend", "id"), "backend_version": g("backend", "version"),
            "protocol": g("backend", "protocol"), "native_session_id": g("backend", "nativeSessionId"),
            "model": g("brain", "model"), "provider": g("brain", "provider"),
            # identity — nullable 그대로
            "id": g("identity", "canonicalCell"),
            "declared": g("identity", "canonicalCell") if g("identity", "joinStatus") == "joined" else None,
            "join_status": g("identity", "joinStatus"), "role": g("identity", "role"),
            "persona": g("identity", "persona"), "workspace": g("identity", "workspace"),
            # usage — null 보존(C2), completeness 라벨 유지
            "in_tok": u.get("inputTokens"), "out_tok": u.get("outputTokens"),
            "cache": u.get("cachedInputTokens"), "reasoning_tok": u.get("reasoningTokens"),
            "total_tok": u.get("totalTokens"), "usage_completeness": u.get("completeness"),
            "requests": u.get("requests"), "responses": u.get("responses"),
            "cost_usd_reported": u.get("costUsd"),
            # outcome/coordination (bench 소재)
            "contributions": g("coordination", "contributions"), "topic": g("coordination", "topic"),
            "publication_phase": g("coordination", "publicationPhase"),
            "session_closed": g("coordination", "sessionClosed"),
            "gate": g("outcome", "gate"), "run_status": g("run", "status"),
            "causal_claim": g("outcome", "causalClaim"), "checks": g("outcome", "checks"),
            # 시간·계보 — parse된 신뢰값을 canonical UTC Z로 정규화(혼합 정밀도의 문자열
            # 비교 함정 방지; raw는 record에 보존)
            "first_ts": (lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None)(
                _parse_ts(g("run", "startedAt"))),
            "last_ts": (lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None)(
                _anchor_dt(r)),
            "timing": g("run", "timingCompleteness"),
            "eval": g("evaluation", "name"), "scenario": g("evaluation", "scenario"),
            "preregistration_id": g("run", "preregistrationId"),
            "prior_failure": g("provenance", "source", "priorFailure"),
            "origin": "reported",  # passive origin(terminal/subagent)과 구분되는 라벨
        })
    recs = sorted(out, key=lambda r: r.get("last_ts") or "")
    if since_days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recs = [r for r in recs if (r.get("last_ts") or "") >= cutoff]
    return recs


# ── correlation link (H1 pair — reported ↔ passive 교차검증) ─────────────────
# 계약(관측 ingest 6·emitter SHIPPED 합의): passive/reported는 별도 evidence이고,
# exact `(backend.id, backend.nativeSessionId)` pair가 passive 관측의 네이티브 store
# session id와 정확히 일치할 때만 링크한다. 추측 매칭(timestamp/cell/workspace 근접)
# 없음. 링크는 **읽기 시점 계산**(무기록) — 양 소스 원본은 그대로 두고 나란히 놓기만
# 한다(교차검증 표면이지 병합·판결 아님; usage 의미도 다르다: broker 계측 vs
# transcript 파생).
#
# backend → passive vendor 매핑 = "그 백엔드가 어느 네이티브 store에 쓰는가"라는
# correlation 전용 지식. ingest의 open backend 축(계약 1)과 무관 — 미지 백엔드도
# ingest는 되고, correlation만 no-mapping으로 정직하게 표면화된다.
_BACKEND_NATIVE_VENDOR = {"claude-code": "claude", "grok-build": "grok"}

_CORR_REPORTED_KEYS = ("model", "provider", "id", "role", "join_status", "run_status",
                       "gate", "in_tok", "out_tok", "cache", "total_tok", "requests",
                       "usage_completeness", "first_ts", "last_ts")
_CORR_PASSIVE_KEYS = ("id", "model", "origin", "declared", "role", "join_status",
                      "in_tok", "out_tok", "cache", "first_ts", "last_ts")


def correlate(state_dir: Path, since_days: float | None = None) -> list:
    """reported run ↔ passive 세션 H1 링크. reported run마다 한 행, link_status =

    - linked: exact pair 일치하는 passive 관측 존재 (양 소스 나란히)
    - no-passive: 매핑은 알지만 passive 관측 없음 — actor-scope 불투명·transcript
      청소 둘 다 정상 사유(오류 아님)
    - no-mapping: 미지 백엔드 — ingest는 유효, 네이티브 store 지식만 없음
    - no-native-id: 백엔드가 stable session 계약 없음(nativeSessionId null, deepcode 등)

    since_days는 **reported 창만** 필터 — passive 대응은 창 무관 탐색(오래 settle된
    passive 스냅샷이 창 밖이라 링크를 놓치면 거짓 no-passive가 된다)."""
    reported = load_reported(state_dir, since_days=since_days)
    passive = {(r.get("vendor"), r.get("session_id")): r for r in load(state_dir)}
    out = []
    for r in reported:
        nsid = r.get("native_session_id")
        vendor = _BACKEND_NATIVE_VENDOR.get(r.get("backend"))
        p = None
        if nsid is None:
            status = "no-native-id"
        elif vendor is None:
            status = "no-mapping"
        else:
            p = passive.get((vendor, nsid))
            status = "linked" if p else "no-passive"
        out.append({
            "run_id": r["run_id"], "backend": r.get("backend"),
            "native_session_id": nsid, "link_status": status,
            "reported": {k: r.get(k) for k in _CORR_REPORTED_KEYS},
            "passive": ({k: p.get(k) for k in _CORR_PASSIVE_KEYS} if p else None),
        })
    return out


def render_correlation(links: list, days: float) -> str:
    """correlation 뷰 — 상태별 요약 + linked는 양 소스 나란히(병합 없음)."""
    n = {s: sum(1 for l in links if l["link_status"] == s)
         for s in ("linked", "no-passive", "no-mapping", "no-native-id")}
    lines = [f"correlation — 최근 {days:g}일 reported {len(links)} run"
             f" · linked {n['linked']} · no-passive {n['no-passive']}"
             f" · no-mapping {n['no-mapping']} · no-native-id {n['no-native-id']}"]
    if not links:
        lines.append("  (reported 관측 없음 — organum observatory ingest 로 시작)")
        return "\n".join(lines)
    for l in links:
        if l["link_status"] != "linked":
            continue
        r, p = l["reported"], l["passive"]
        lines.append(f"  {l['run_id'][:20]}…  {l['backend']} · {l['native_session_id']}")
        comp = f" ({r['usage_completeness']})" if r.get("usage_completeness") else ""
        lines.append(f"    reported: {r.get('model') or '—'} · in {_fmt_tok(r.get('in_tok'))}"
                     f" · out {_fmt_tok(r.get('out_tok'))} · cache {_fmt_tok(r.get('cache'))}{comp}"
                     f" · role {r.get('role') or '—'}")
        lines.append(f"    passive : {p.get('model') or '—'} · in {_fmt_tok(p.get('in_tok'))}"
                     f" · out {_fmt_tok(p.get('out_tok'))} · cache {_fmt_tok(p.get('cache'))}"
                     f" · {p.get('origin') or '—'} · role {p.get('role') or '—'}")
    lines.append("  (usage 의미 상이: reported=broker 계측 · passive=transcript 파생 — 병합·판결 없음)")
    return "\n".join(lines)
