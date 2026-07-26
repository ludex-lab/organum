"""organum health — substrate-health 감시 (면역 감지 tier).

발단: 2026-07-25 grok CLI recap 폭주 실사고 — `recap_requests/`에 ~61MB 전체-대화
스냅샷이 분 단위 반복 기록(868개=48G), 디스크 90G→3G, 머신 메모리 압박 리부트.
근본 수정은 벤더(xAI) 몫. organum의 자리는 **감지+신호**다: 벤더 native store의
이상 성장을 벤더-중립으로 감시하고(관제탑), 케어테이커(human/chief)가 assert하면
기존 alarm(우선순위 경보)·relay(문제 셀 지향 통지, escalate) 프리미티브로 전파한다.

경계(헌법):
- 관측은 read-only — 벤더 파일 불변. **자동 삭제·프로세스 kill 없음** (effector는
  actor층/human — 청소 소유권 규율 존중).
- 탐지지 판결 아님 — 큰 세션은 정상일 수 있다(evidence). 경보 발동은 alarm 규율
  그대로 human/chief만: assert = 탐지→경보의 **사람 게이트**.
- 무데몬 — checkup(읽기 진단)·`observatory health`/sync(측정 영속)가 편승.

이력: 상태 전이만 `observatory/health.jsonl`에 append(integrity.jsonl 동형 —
면역 기억의 시간축). 직전 측정 캐시 `health-last.json`은 성장 속도용 작업 파일
(evidence 아님, 덮어쓰기)."""

from __future__ import annotations

import datetime
import json
import os
import shutil
from pathlib import Path
from urllib.parse import unquote

# 문턱 — 실사고 실측 기준. 세션 단위(MB): 정상 대형 세션과 폭주 사이.
SESSION_WARN_MB = 256.0
SESSION_ALERT_MB = 1024.0
GROWTH_ALERT_MB_MIN = 10.0    # 성장 속도(MB/분) — 폭주 실측 ~61MB/분
GROWTH_MIN_DELTA_MB = 32.0    # 속도 판정 최소 증가폭(짧은 간격 측정 노이즈 가드)
DISK_WARN_GB = 20.0
DISK_ALERT_GB = 8.0

# 면역 기억(항체): 알려진 병리 시그니처 — 일반 문턱보다 낮게, 존재+크기로 조기 경보.
# grok recap 문턱(64/512MiB)은 organum-code supervisor가 actor-private GROK_HOME 검사
# (grok-runtime-health/v1, launch fail-closed·settle 재검사, organum-code dbfb516)에 **동일
# 값으로 채택** — 항체가 층 경계를 넘어 공유된 계약. 값 변경 시 그쪽과 조율할 것.
# 분업: host store·machine-wide(디스크)=여기(substrate-health 정본) / actor-private runtime=
# organum-code(우리 불가시 — 격리가 목적). 양쪽 다 effector 없음(no delete/kill).
SIGNATURES = [
    {"vendor": "grok", "dirname": "recap_requests", "warn_mb": 64.0, "alert_mb": 512.0,
     "note": "xAI grok CLI recap 스냅샷 폭주 (2026-07-25 실사고: ~61MB/분×868=48G·머신 리부트)"},
]

LAST_FILE = "health-last.json"
LOG_FILE = "health.jsonl"
_MB = 1024.0 * 1024.0


class HealthError(Exception):
    """substrate-health 규율 위반 (finding 없는 assert 등)."""


def store_roots() -> list[tuple[str, Path]]:
    """벤더 native store 루트 — 어댑터 discover 경로와 동일 소스(HOME).
    머신-전역: 폭주는 다른 프로젝트 세션에서도 이 머신을 죽인다(실사고가 그랬다)."""
    from organum.adapters import HOME
    return [("claude", HOME / ".claude" / "projects"),
            ("codex", HOME / ".codex" / "sessions"),
            ("agy", HOME / ".gemini" / "antigravity-cli"),
            ("grok", HOME / ".grok" / "sessions")]


# 세션 단위 노드의 루트-상대 depth — 벤더 저장 구조가 다르다(실측이 잡은 함정: codex는
# YYYY/MM이 depth 2라 제네릭 depth-2는 "정상 한 달 누적"을 폭주로 오인).
# claude: <proj>/<uuid>.jsonl·<proj>/<uuid>/(subagents) = 2 · codex: YYYY/MM/DD/rollout-*.jsonl = 4
# grok: <enc-cwd>/<sid>/ = 2 · agy: brain|conversations/<uuid> = 2
UNIT_DEPTH = {"claude": 2, "codex": 4, "grok": 2, "agy": 2}


def _unit_nodes(root: Path, depth: int) -> list[Path]:
    """루트에서 정확히 depth 아래의 노드(dir·file) = 세션 단위.

    symlink 자식은 어느 단계서든 미추적(계약) — store 내부 symlink가 가리키는 외부
    디렉터리/파일이 세션 단위로 오인·오경보되는 것 차단(critic blocker: 외부 2GiB 파일
    반례). 루트 자체는 예외(_tree_sizes의 os.walk 최상위 동작과 동일 — store 위치가
    symlink인 정상 셋업 허용)."""
    cur = [root]
    for _ in range(depth):
        nxt: list[Path] = []
        for d in cur:
            if d.is_dir():
                try:
                    nxt.extend(p for p in d.iterdir() if not p.is_symlink())
                except OSError:
                    continue
        cur = nxt
    return cur


def _tree_sizes(root: Path) -> dict:
    """root 이하 모든 디렉터리의 누적 bytes (한 번 walk, symlink 미추적).
    topdown=False라 자식 dir 합이 이미 계산돼 있다 — dirnames로 O(N) 합산
    (전체 dict 재스캔은 O(N²)로 실측 3분+ — 실경로 수만 파일에서 치명)."""
    sizes: dict[Path, int] = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        d = Path(dirpath)
        total = 0
        for fn in filenames:
            try:
                total += (d / fn).lstat().st_size
            except OSError:
                continue
        for dn in dirnames:
            total += sizes.get(d / dn, 0)
        sizes[d] = total
    return sizes


def _identity_of(vendor: str, root: Path, path: Path):
    """finding 경로 → (session_id, cwd_hint) — 파생 가능한 벤더만(grok dir·claude file).
    불가면 (None, None) 정직 유지 — 추측 없음."""
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        return None, None
    if vendor == "grok" and len(rel) >= 2:
        return rel[1], unquote(rel[0])
    if vendor == "claude" and len(rel) >= 2 and rel[-1].endswith(".jsonl"):
        return Path(rel[-1]).stem, None   # 프로젝트 dir mangling은 비가역 — cwd 추측 안 함
    if vendor == "codex" and rel and rel[-1].endswith(".jsonl"):
        import re
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rel[-1])
        return (m.group(0) if m else None), None
    return None, None


def _flag(severity_mb: float, warn: float, alert: float):
    if severity_mb >= alert:
        return "alert"
    if severity_mb >= warn:
        return "warn"
    return None


def measure(state_dir: Path | None = None, roots: list | None = None,
            persist: bool = False, disk_free_gb: float | None = None,
            now: datetime.datetime | None = None) -> dict:
    """전 벤더 store 측정 → {findings, totals, disk_free_gb, ts}.

    findings kind: store-size(세션 단위 초과) · signature(항체 일치) ·
    store-growth(직전 측정 대비 속도 — 캐시 있을 때만) · disk-low.
    persist=True면 캐시 갱신 + 상태 전이를 health.jsonl에 append(변화 시만·state_dir 필요).
    read-only 관측: 벤더 파일은 읽지도 않는다 — lstat 크기만."""
    roots = roots if roots is not None else store_roots()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    findings: list[dict] = []
    totals: dict[str, float] = {}
    node_mb: dict[str, float] = {}          # 성장 추적 단위(depth≤2 노드+루트)

    for vendor, root in roots:
        if not root.is_dir():
            continue
        sizes = _tree_sizes(root)
        totals[vendor] = round(sizes.get(root, 0) / _MB, 1)
        node_mb[str(root)] = totals[vendor]
        depth = UNIT_DEPTH.get(vendor, 2)
        # 성장 추적: 세션 단위까지의 중간 dir도 기록(다수 세션에 분산된 성장은 부모에서 보인다)
        level = [root]
        for _ in range(depth - 1):
            nxt: list[Path] = []
            for d in level:
                if d.is_dir():
                    try:
                        # symlink 미추적 — 성장 추적 노드도 store 밖 경로가 못 들어온다 (critic blocker)
                        nxt.extend(p for p in d.iterdir()
                                   if p.is_dir() and not p.is_symlink())
                    except OSError:
                        continue
            for p in nxt:
                mb = sizes.get(p, 0) / _MB
                if mb > 1.0:
                    node_mb[str(p)] = round(mb, 1)
            level = nxt
        # 크기 판정은 **세션 단위 노드만** — 정상 누적(월 폴더 등)을 폭주로 오인하지 않는다
        for node in sorted(_unit_nodes(root, depth)):
            try:
                mb = (sizes.get(node, 0) if node.is_dir() else node.lstat().st_size) / _MB
            except OSError:
                continue
            if mb > 1.0:
                node_mb[str(node)] = round(mb, 1)
            sev = _flag(mb, SESSION_WARN_MB, SESSION_ALERT_MB)
            if sev:
                sid, cwd_hint = _identity_of(vendor, root, node)
                findings.append({"kind": "store-size", "severity": sev, "vendor": vendor,
                                 "path": str(node), "mb": round(mb, 1), "rate_mb_min": None,
                                 "session_id": sid, "cwd_hint": cwd_hint,
                                 "note": "세션 단위 store 크기 초과"})
        # 항체 시그니처 — 이름 일치 디렉터리는 낮은 문턱으로 조기 경보
        for sig in SIGNATURES:
            if sig["vendor"] != vendor:
                continue
            for d, b in sizes.items():
                if d.name != sig["dirname"]:
                    continue
                mb = b / _MB
                sev = _flag(mb, sig["warn_mb"], sig["alert_mb"])
                if sev:
                    sid, cwd_hint = _identity_of(vendor, root, d)
                    findings.append({"kind": "signature", "severity": sev, "vendor": vendor,
                                     "path": str(d), "mb": round(mb, 1), "rate_mb_min": None,
                                     "session_id": sid, "cwd_hint": cwd_hint,
                                     "note": f"[항체] {sig['note']}"})

    # 성장 속도 — 직전 측정 캐시가 있을 때만(첫 측정은 baseline)
    prev = _load_last(state_dir) if state_dir else None
    if prev:
        prev_ts = _parse_iso(prev.get("ts"))
        mins = ((now - prev_ts).total_seconds() / 60.0) if prev_ts else None
        if mins and mins >= 1.0:
            hits: list[dict] = []
            for pstr, mb in node_mb.items():
                old = (prev.get("mb") or {}).get(pstr)
                if old is None:
                    continue
                delta = mb - old
                rate = delta / mins
                if delta >= GROWTH_MIN_DELTA_MB and rate >= GROWTH_ALERT_MB_MIN:
                    vendor = next((v for v, r in roots if pstr.startswith(str(r))), "?")
                    sid, cwd_hint = None, None
                    rt = next((r for v, r in roots if pstr.startswith(str(r))), None)
                    if rt is not None:
                        sid, cwd_hint = _identity_of(vendor, rt, Path(pstr))
                    hits.append({"kind": "store-growth", "severity": "alert",
                                 "vendor": vendor, "path": pstr, "mb": mb,
                                 "rate_mb_min": round(rate, 1),
                                 "session_id": sid, "cwd_hint": cwd_hint,
                                 "note": f"{mins:.0f}분간 +{delta:.0f}MB — 폭주 의심"})
            # 가장 구체적인 성장만 — 자식이 걸리면 조상(루트·중간 dir)은 억제(같은 성장의 메아리)
            findings.extend(h for h in hits if not any(
                o["path"] != h["path"] and o["path"].startswith(h["path"] + os.sep)
                for o in hits))

    # 디스크 여유 — 머신 생존 축(실사고: 메모리 압박 리부트 전 디스크가 먼저 신호)
    if disk_free_gb is None:
        try:
            disk_free_gb = shutil.disk_usage(Path.home()).free / (1024 ** 3)
        except OSError:
            disk_free_gb = None
    if disk_free_gb is not None:
        sev = ("alert" if disk_free_gb <= DISK_ALERT_GB
               else "warn" if disk_free_gb <= DISK_WARN_GB else None)
        if sev:
            findings.append({"kind": "disk-low", "severity": sev, "vendor": None,
                             "path": str(Path.home()), "mb": None, "rate_mb_min": None,
                             "session_id": None, "cwd_hint": None,
                             "note": f"디스크 여유 {disk_free_gb:.1f}G"})

    report = {"ts": ts, "findings": findings, "totals": totals,
              "disk_free_gb": round(disk_free_gb, 1) if disk_free_gb is not None else None}
    if persist and state_dir is not None:
        _persist(state_dir, report, node_mb)
    return report


def _parse_iso(v):
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _health_dir(state_dir: Path) -> Path:
    from organum.observatory import DIR_NAME
    return state_dir / DIR_NAME


def _load_last(state_dir: Path) -> dict | None:
    p = _health_dir(state_dir) / LAST_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _log_last_states(state_dir: Path) -> dict:
    """health.jsonl append 순서상 (kind, path) → 마지막 severity."""
    p = _health_dir(state_dir) / LOG_FILE
    out: dict = {}
    if not p.is_file():
        return out
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(r, dict) and r.get("kind") and r.get("path"):
                out[(r["kind"], r["path"])] = r.get("severity")
    except OSError:
        pass
    return out


def _persist(state_dir: Path, report: dict, node_mb: dict) -> int:
    """캐시 덮어쓰기 + 상태 전이만 append (변화 없으면 로그 안 커짐). 반환: 전이 수."""
    d = _health_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / LAST_FILE).write_text(
        json.dumps({"ts": report["ts"], "mb": node_mb}, ensure_ascii=False), encoding="utf-8")
    last = _log_last_states(state_dir)
    cur = {(f["kind"], f["path"]): f for f in report["findings"]}
    n = 0
    with open(d / LOG_FILE, "a", encoding="utf-8") as fh:
        for key, f in cur.items():
            if last.get(key) != f["severity"]:
                fh.write(json.dumps({"ts": report["ts"], **{k: f.get(k) for k in
                         ("kind", "severity", "vendor", "path", "mb", "rate_mb_min", "note")}},
                         ensure_ascii=False) + "\n")
                n += 1
        for key, sev in last.items():
            if sev != "ok" and key not in cur:   # 해소 전이 — 사건의 끝도 이력이다
                fh.write(json.dumps({"ts": report["ts"], "kind": key[0], "path": key[1],
                                     "severity": "ok"}, ensure_ascii=False) + "\n")
                n += 1
    return n


def resolve_cell(state_dir: Path, vendor: str | None, session_id: str | None) -> str | None:
    """finding → 선언 셀(canonical id) — passive 관측의 declared 조인만 사용(추측 없음)."""
    if not vendor or not session_id:
        return None
    from organum.observatory import load
    for r in load(state_dir):
        if r.get("vendor") == vendor and r.get("session_id") == session_id and r.get("declared"):
            return r["declared"]
    return None


def assert_finding(cwd: Path, state_dir: Path, path: str, frm: str = "human",
                   level: str = "notice", to: str | None = None, note: str = "") -> dict:
    """케어테이커 assert: 활성 finding을 확정 → alarm(우선순위 경보, human/chief 게이트) +
    문제 셀이 식별되면 지향 escalate 편지(문제를 그 에이전트에게 알림).

    탐지→경보 사이의 **사람 게이트**: measure가 스스로 경보를 울리지 않는다(자동 판결 금지).
    반환 {alarm, letter, target}. finding 없는 경로는 HealthError(추측 경보 방지)."""
    from organum import alarm as _alarm
    from organum import relay as _relay
    report = measure(state_dir)
    hit = next((f for f in report["findings"] if f["path"] == path), None)
    if hit is None:
        raise HealthError(f"활성 finding 없음: {path} — 'observatory health'로 현재 목록 확인")
    rate = f" · +{hit['rate_mb_min']}MB/분" if hit.get("rate_mb_min") else ""
    size = f" · {hit['mb']}MB" if hit.get("mb") is not None else ""
    body = (f"[substrate-health {hit['severity'].upper()}] {hit['kind']}"
            f" · {hit['vendor'] or '—'}{size}{rate}\n"
            f"경로: {hit['path']}\n{hit['note']}"
            + (f"\n케어테이커 메모: {note}" if note else "")
            + "\n(read-only 관측 — 청소·중단은 소유권자/actor층 몫. 탐지지 판결 아님.)")
    from_id = "" if (frm or "").strip().lower() == "human" else frm
    alarm_f = _alarm.sound(cwd, state_dir, body, frm=frm, to="all", level=level,
                           src="health-assert", from_id=from_id)
    target = to or resolve_cell(state_dir, hit.get("vendor"), hit.get("session_id"))
    letter_f = None
    if target:
        letter = (f"{target}에게 — 네 발밑(벤더 store)에서 이상 성장이 관측된다.\n\n{body}\n\n"
                  "네 잘못이 아닐 수 있다(하네스 버그 가능). 부탁: ① 아주 긴 작업은 세션 쪼개기 "
                  "고려 ② 재발 목격 시 경로·타임스탬프 기록 ③ 세션 본체 파일은 손대지 말 것 — "
                  "청소는 소유권자와 합의된 라인만.")
        letter_f = _relay.send(cwd, letter, frm=frm, to=target, topic="substrate-health",
                               escalate=True, src="health-assert", from_id=from_id)
    return {"alarm": alarm_f, "letter": letter_f, "target": target}


def render(report: dict) -> str:
    f = report["findings"]
    n_a = sum(1 for x in f if x["severity"] == "alert")
    n_w = sum(1 for x in f if x["severity"] == "warn")
    tot = " · ".join(f"{v} {mb:g}MB" for v, mb in sorted(report["totals"].items()))
    disk = (f" · 디스크 여유 {report['disk_free_gb']:g}G"
            if report.get("disk_free_gb") is not None else "")
    lines = [f"substrate-health — 긴급 {n_a} · 경고 {n_w}{disk}", f"  store: {tot or '(없음)'}"]
    icon = {"alert": "✗", "warn": "⚠"}
    for x in f:
        rate = f" · +{x['rate_mb_min']}MB/분" if x.get("rate_mb_min") else ""
        size = f" · {x['mb']}MB" if x.get("mb") is not None else ""
        lines.append(f"  {icon[x['severity']]} [{x['kind']}] {x['vendor'] or '—'}{size}{rate}"
                     f" · {x['path']}\n      {x['note']}")
    if f:
        lines.append("  → 케어테이커 assert: organum observatory health --assert <경로>"
                     " [--to <cell>] [--level pause]  (human/chief만 — 경보+지향 통지)")
    else:
        lines.append("  (이상 없음)")
    return "\n".join(lines)
