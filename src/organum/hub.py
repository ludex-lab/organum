"""organum hub — 크로스-워크스페이스 조율 필드 (홈레벨).

프로젝트-로컬 relay/agora와 **별개로**, 서로 다른 프로젝트의 셀이 `persona@workspace`로 핀포인트
편지를 주고받는 substrate. 파워유저(Orca) 통찰: 통신 식별자가 (에이전트 × 워크스페이스) 복합이어야
깨끗한 라우팅 — 프로젝트 A의 X가 B의 X 소리를 안 듣게(few-shot poisoning 방벽).

**계약(organum↔organum-code 정렬, docs/cross-workspace-hub-v0.md)**:
- broadcast 없음(addressed-only) = 핀포인트 강제.
- **send 시점에 alias→to_id(cell_key)+epoch 확정**해 봉투에 고정 — inbox에서 재-resolve 안 함(재등록
  오배송 차단). routing authority = 확정 `to_id`.
- inbox = bounded·무손실·비소비 `{items, next_cursor, has_more}` (opaque cursor). 명시 per-file ACK.
- stable `event_id`(파일명과 별개, local relay와 네임스페이스 분리). idem 재전송 dedup.
- 0개/복수 resolve·rebound·같은 cell 다른 persona = fail-closed.
- frozen `cell_key`는 안 건드리고 persona/workspace는 선언 metadata(R5 cell 파생 입력 아님).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from organum import field as _f
from organum import state as st

RELAY_FIELD = "hub/relay"  # field_dir(base, "hub/relay") = base/.organum/hub/relay
DEFAULT_LIMIT = 20


def _base() -> Path:
    """허브 베이스 — ORGANUM_HUB(테스트·멀티허브·launcher 검증 옵션) 또는 홈. 필드는 <base>/.organum/hub/."""
    env = os.environ.get("ORGANUM_HUB")
    return Path(env) if env else Path.home()


def _registry_dir() -> Path:
    return _f.field_dir(_base(), "hub/registry")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── registry — (persona, workspace) → cell_key, epoch/liveness ────────────────

class HubError(ValueError):
    """허브 계약 위반 — conflict/ambiguous/rebind 등 (fail-closed)."""


def register(cell_id: str, persona: str, workspace: str, project_path: str, role: str) -> dict:
    """허브 등록(명시 opt-in). 반환 = registration echo. 규칙:
    - 같은 cell이 **다른 persona/workspace**로 재등록 = HubError(조용한 rebind 금지; 명시 leave→재등록 필요).
    - 같은 cell·같은 선언 재등록(resume) = epoch 보존·last_seen 갱신(수렴).
    - 신규 = 새 epoch(불투명 uuid). persona는 valid_cell_id(호출자 검증), workspace는 cell_key 정규화."""
    ck, pk, wk = st.cell_key(cell_id), st.cell_key(persona), st.cell_key(workspace)
    existing = registry_of(cell_id)
    if existing and (existing.get("persona") != pk or existing.get("workspace") != wk):
        raise HubError(
            f"셀 '{ck}'가 이미 {existing.get('persona')}@{existing.get('workspace')}로 등록됨 — "
            f"{pk}@{wk}로 조용한 재등록 금지. 'organum hub leave' 후 재등록(명시 rebind).")
    epoch = existing.get("epoch") if existing else uuid.uuid4().hex[:12]
    rec = {
        "cell_key": ck, "persona": pk, "workspace": wk,
        "project_path": str(project_path), "role": role,
        "epoch": epoch,
        "registered_at": existing.get("registered_at") if existing else _now(),
        "last_seen": _now(),
    }
    d = _registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, d / f"{ck}.json")
    return rec


def deregister(cell_id: str) -> bool:
    """허브 등록 해제(명시 reap) — persona@workspace 슬롯을 비운다. 세션 종료·rebind 전에."""
    p = _registry_dir() / f"{st.cell_key(cell_id)}.json"
    if p.is_file():
        p.unlink()
        return True
    return False


def registry_all() -> list[dict]:
    d = _registry_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def registry_of(cell_id: str) -> dict | None:
    p = _registry_dir() / f"{st.cell_key(cell_id)}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def resolve(persona: str, workspace: str) -> list[dict]:
    """persona@workspace → 등록 항목들. MVP: staleness/lease는 hardening(백로그) — 지금은 전량 반환하고
    ≥2면 send가 ambiguous fail. reap은 명시 deregister/leave."""
    pk, wk = st.cell_key(persona), st.cell_key(workspace)
    return [e for e in registry_all() if e.get("persona") == pk and e.get("workspace") == wk]


# ── send — alias를 send 시점에 to_id+epoch로 확정 ───────────────────────────

def mark_join(cell_id: str, reset: bool = False) -> str:
    return _f.mark_join(_base(), RELAY_FIELD, cell_id, reset=reset)


def _resolve_target(to: str) -> dict:
    """수신 주소 → 확정 대상 dict {to_id, address, persona, workspace, epoch}. broadcast/0/복수 = fail-closed."""
    raw = (to or "").strip()
    if not raw or raw.lower() == "all" or raw == "*":
        raise HubError("허브는 broadcast 없음 — persona@workspace 또는 cell_key 하나로 지정하세요.")
    if "," in raw:
        raise HubError("허브 send는 단일 대상만 — 다중은 각각 send(핀포인트).")
    if "@" in raw:
        p, _, w = raw.partition("@")
        matches = resolve(p, w)
        if not matches:
            raise HubError(f"'{st.cell_key(p)}@{st.cell_key(w)}'에 등록된 live 셀 없음 (fail-closed).")
        if len(matches) > 1:
            raise HubError(
                f"'{st.cell_key(p)}@{st.cell_key(w)}'가 {len(matches)}개 셀로 모호 — cell_key로 직접 지정하세요.")
        e = matches[0]
        if not e.get("epoch"):  # 빈-epoch registration = 손상/무효 → fail-closed(critic 재감사 A1)
            raise HubError(f"'{st.cell_key(p)}@{st.cell_key(w)}' registration에 epoch 없음 — 무효(fail-closed).")
        return {"to_id": e["cell_key"], "address": f"{e['persona']}@{e['workspace']}",
                "persona": e["persona"], "workspace": e["workspace"], "epoch": e["epoch"]}
    # raw cell_key 직접 지정 — **현재 등록된 live cell만**(critic A1: 미등록=빈 epoch 봉투가 다음
    # registration에 wildcard로 새는 것 차단). 확정 to_id+nonempty epoch를 registry에서.
    ck = st.cell_key(raw)
    if not st.valid_cell_id(raw):
        raise HubError(f"cell_key '{raw}' 계약 위반 — ASCII [A-Za-z0-9._-] 1~40자, 선/후행 점 금지.")
    reg = registry_of(ck)
    if not reg or not reg.get("epoch"):
        raise HubError(
            f"cell '{ck}'가 허브 미등록 — send 불가(fail-closed). 대상이 먼저 'organum join --persona' 필요.")
    return {"to_id": ck, "address": ck, "persona": reg.get("persona", ""),
            "workspace": reg.get("workspace", ""), "epoch": reg["epoch"]}


def send(body: str, frm: str, from_id: str, to: str, topic: str = "",
         thread: str = "", reply_to: str = "", idem_key: str = "") -> dict | None:
    """크로스-워크스페이스 addressed 편지. send 시점에 alias→to_id+epoch 확정해 봉투에 고정.
    반환 = receipt {file, event_id, from_id, idem, to:{...}} 또는 빈 본문이면 None."""
    if not (body or "").strip():
        return None
    tgt = _resolve_target(to)  # fail-closed: broadcast/0/복수/invalid
    sender = registry_of(from_id) if from_id else None
    event_id = uuid.uuid4().hex
    extra = {
        "to_id": tgt["to_id"], "to_address": tgt["address"],
        "to_persona": tgt["persona"], "to_workspace": tgt["workspace"], "to_epoch": tgt["epoch"],
        "from_persona": sender.get("persona", "") if sender else "",
        "from_workspace": sender.get("workspace", "") if sender else "",
        "event_id": event_id,
    }
    try:
        fn = _f.post(_base(), RELAY_FIELD, body, frm=frm, to=tgt["to_id"], from_id=from_id,
                     src="hub-cli", topic=topic, thread=thread, reply_to=reply_to,
                     idem_key=idem_key, extra=extra)
    except _f.PostConflict as e:  # 같은 idem·다른 payload = conflict(fail-closed)
        raise HubError(str(e))
    if not fn:
        return None
    # idem-hit이면 기존 봉투의 확정값을 정본으로 되읽는다(내 후보 event_id는 버려질 수 있다).
    meta = _f.get_meta(_base(), RELAY_FIELD, fn) or {}
    return {
        "file": fn, "event_id": meta.get("event_id", event_id),
        "from_id": meta.get("from_id", ""), "idem": meta.get("idem", ""),
        "to": {"address": meta.get("to_address", tgt["address"]), "cell": meta.get("to_id", tgt["to_id"]),
               "persona": meta.get("to_persona", ""), "workspace": meta.get("to_workspace", ""),
               "epoch": meta.get("to_epoch", "")},
    }


# ── inbox — bounded·무손실·비소비 (opaque cursor) ────────────────────────────

def _item(m: dict) -> dict:
    return {
        "event_id": m.get("event_id", ""), "file": m["file"],
        "from_id": m.get("from_id", ""), "from": m.get("from", "?"),
        "from_persona": m.get("from_persona", ""), "from_workspace": m.get("from_workspace", ""),
        "to_address": m.get("to_address", ""), "to_id": m.get("to", ""),
        "to_persona": m.get("to_persona", ""), "to_workspace": m.get("to_workspace", ""),
        "to_epoch": m.get("to_epoch", ""),
        "ts": m.get("ts", ""), "topic": m.get("topic", ""), "idem": m.get("idem", ""),
        "thread": m.get("thread", ""), "in_reply_to": m.get("in_reply_to", ""),
        "escalate": (m.get("escalate", "").lower() == "true"),
        "body": (m.get("body") or "").strip()[:4000],
    }


def _sort_key(m: dict) -> tuple:
    """oldest-first 정렬 키 = (mtime, file). 파일명만 쓰면 같은 초의 `-2.md`가 `.md`보다 앞서는
    lexicographic 함정('-'<'.')이 있어 mtime을 1차 키로. cursor 비교도 같은 키를 쓴다."""
    return (m["_mtime"], m["file"])


def _matched(cell_id: str, include_read: bool) -> list[dict]:
    """확정 to_id==내 cell_key(+epoch 일치)인 안 읽은 편지, 오래된 순. broadcast 없음.
    epoch: 봉투 to_epoch가 있으면 내 현재 registration epoch와 일치할 때만(재등록/rebind 오배송 차단)."""
    base, field = _base(), RELAY_FIELD
    me = st.cell_key(cell_id)
    reg = registry_of(cell_id)
    my_epoch = reg.get("epoch") if reg else None
    read = set() if include_read else _f.read_set(base, field, cell_id)
    join = _f.join_ts(base, field, cell_id)
    d = _f.field_dir(base, field)
    out = []
    if not d.is_dir():
        return out
    for p in d.glob("*.md"):
        try:
            meta, body = _f.parse_msg(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if (meta.get("to") or "").strip() != me:  # 확정 to_id 정확 매칭(broadcast 없음)
            continue
        # epoch: **양쪽 nonempty + 정확 일치만**(critic 재감사 A1). None(미등록 수신)과 ""(빈 epoch
        # 봉투)를 ""로 합치면 미등록 수신·빈-epoch 봉투가 서로 통과 → 유효 epoch=nonempty 토큰으로 못박음.
        to_epoch = (meta.get("to_epoch") or "").strip()
        if not to_epoch or not my_epoch or to_epoch != my_epoch:
            continue
        if join and (meta.get("ts") or "") < join:
            continue
        fid = (meta.get("from_id") or "").strip()
        if fid and st.cell_key(fid) == me:  # 자기 글 제외
            continue
        if p.name in read:
            continue
        meta["file"] = p.name
        meta["_mtime"] = p.stat().st_mtime
        meta["body"] = body
        out.append(meta)
    out.sort(key=_sort_key)
    return out


def inbox(cell_id: str, cursor: str | None = None, limit: int = DEFAULT_LIMIT,
          include_read: bool = False) -> dict:
    """내 허브 편지 — 확정 to_id로만 매칭, bounded·무손실·**비소비**. opaque cursor(=마지막 파일명)로
    oldest-first traversal. 반환 {items, next_cursor, has_more}. hard recent-N horizon 없음."""
    matched = _matched(cell_id, include_read)
    if cursor:  # cursor 파일의 정렬키 이후만(파일이 ACK돼 read_set에 있어도 디스크엔 남아 mtime 읽힘)
        cpath = _f.field_dir(_base(), RELAY_FIELD) / cursor
        ckey = (cpath.stat().st_mtime, cursor) if cpath.is_file() else (0.0, cursor)
        matched = [m for m in matched if _sort_key(m) > ckey]
    limit = max(1, int(limit))
    page = matched[:limit]
    has_more = len(matched) > limit
    return {
        "items": [_item(m) for m in page],
        "next_cursor": (page[-1]["file"] if (page and has_more) else None),
        "has_more": has_more,
    }


def mark_read(cell_id: str, filename: str) -> dict:
    """허브 편지 읽음 표시(명시 per-file semantic ACK) — **authorization 검증**: 파일이 실제 존재하는
    안전한 허브 봉투이고, 확정 `to_id`가 나이며, `to_epoch`가 내 현재 registration epoch와 같아야 ACK.
    불일치/미존재 = HubError(fail-closed). 유효 ACK 재시도만 idempotent. receipt 반환."""
    base, field = _base(), RELAY_FIELD
    me = st.cell_key(cell_id)
    # 경로 주입 차단 + 존재 확인 (field.archive의 경로 가드와 동일 결)
    if not (filename.endswith(".md") and "/" not in filename and "\\" not in filename
            and not filename.startswith(".")):
        raise HubError(f"허브 편지 파일명 '{filename}' 부적격.")
    p = _f.field_dir(base, field) / filename
    if not p.is_file():
        raise HubError(f"허브 편지 '{filename}' 없음 — ACK 불가(fail-closed).")
    try:
        meta, _ = _f.parse_msg(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise HubError(f"허브 편지 '{filename}' 읽기 실패 — {e}")
    if (meta.get("to") or "").strip() != me:  # 확정 to_id authorization
        raise HubError(f"편지 '{filename}'는 이 셀({me}) 대상이 아님 — ACK 권한 없음.")
    to_epoch = (meta.get("to_epoch") or "").strip()
    reg = registry_of(cell_id)
    my_epoch = reg.get("epoch") if reg else None
    # 양쪽 nonempty + 정확 일치만(critic 재감사 A1: None·"" 합치기 금지 — 미등록 수신의 빈-epoch ACK 차단)
    if not to_epoch or not my_epoch or to_epoch != my_epoch:
        raise HubError(
            f"편지 '{filename}' epoch({to_epoch or '없음'}) ≠ 현재 등록 epoch({my_epoch or '없음(미등록)'}) "
            "— ACK 불가(fail-closed).")
    already = filename in _f.read_set(base, field, cell_id)
    _f.mark_read(base, field, cell_id, filename)
    return {"file": filename, "event_id": meta.get("event_id", ""), "for_id": me,
            "to_epoch": to_epoch, "read": True, "already_read": already}
