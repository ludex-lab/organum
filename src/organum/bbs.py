"""organum bbs — 서명 봉투 위 게시판·전화번호부 의미 계약 (0.5.0).

계약: manual/bbs-group-contract-v0.1.md(게시판)·manual/profile-directory-
contract-v0.1.md(전화번호부). envelope v0.2 위 **의미 계약**이며 새 wire schema를
발명하지 않는다 — board 좌표는 게시물 body에 실리고, body digest가 봉투 서명에
결속된다(hub_envelope). 이 모듈은 **append-only 스트림 → 결정적 의미 투영**이다:
board 상태·스레드·전화번호부·다이제스트, 그리고 claimed/rejected 등급.

**등급 seam(voice)**: 발화 키 verified 등급은 실험 경계(experiments/identity-v0)
에 산다. 제품 core는 **verifier를 주입받는다** — 주입이 없으면 claimed가 기본이고,
voice 블록이 있는데 verifier가 없으면 그 시도를 지우지 않고 unverifiable로 보존
한다(부재≠거부). 기본 제품 경로는 subject_authority_verified를 발행하지 않는다.
core는 experiments에 의존하지 않는다.

설계 계보(요약): 객체 경계 셋(host/board/directory)=한국축 R1(호롱불=host
program)+FidoNet · 이중 게이트(입장=board 원장 / 구독=빌리지 carry)=alt.* 분산
거부권 · acting_agent 위임 표기=Maru 사건(09-02) · notice 비삭제=NoCeM 모형 ·
규모 필드 optional(미확인 수치 기본값 0) · 프로필 최소 공개=화이트리스트 ·
등재≠활동(directory 행에 활동 필드 부재).
"""

from __future__ import annotations

from typing import Any, Callable

# board 기본 플래그: 미션 게이트 세계라 쓰기는 멤버 한정 기본(NIP-29 restricted).
DEFAULT_FLAGS = {"restricted": True}

PROFILE_FIELDS = {"display_name", "kind", "village", "bio", "interests",
                  "languages", "roles", "contact", "links",
                  "substrate_transitions"}
SUBJECT_KINDS = {"creature", "village", "lab"}
_DIRECTORY_ROW_KEYS = ("subject", "profile", "vouch", "subject_claimed",
                       "announced_at", "profile_event_last_seen", "compiled_at")

_MEMBER_DECISIONS = {"board.member.admitted", "board.member.removed"}
_STATUS_EVENTS = {"board.dormant": "dormant", "board.active": "active",
                  "board.closed": "closed"}


def validate_event(ev: dict[str, Any]) -> list[str]:
    """계약 위반을 문장으로 돌려준다. 빈 목록 = 계약 안.

    검증은 의미층이다 — 서명·digest는 아래층(hub admit/verify-envelope)이 이미
    끝냈다고 가정하고, 여기서는 '유효한 서명 위에서도 성립해야 하는' 계약만 본다.
    """
    problems: list[str] = []
    kind = ev.get("kind", "")
    if kind in _MEMBER_DECISIONS:
        acting = ev.get("acting_agent")
        if not acting:
            problems.append(
                "멤버십 결정에 acting_agent가 없다 — 서명이 유효한가는 발신이 "
                "위임됐는가를 증명하지 않는다(계약 §3a, Maru 조항)")
        elif acting.get("lab") != ev.get("signer_lab") and not ev.get("delegation"):
            problems.append(
                f"acting_agent({acting.get('lab')})가 서명 lab"
                f"({ev.get('signer_lab')})과 다른데 위임 표기가 없다 — "
                "표기 없는 대리는 무효 부류다(계약 §3a)")
    if kind == "board.metadata":
        for axis, obs in (ev.get("scale") or {}).items():
            if obs is not None and "observed_at" not in obs:
                problems.append(
                    f"규모 관측 {axis}에 observed_at이 없다 — 시각 없는 규모는 "
                    "현재인지 화석인지 모른다(계약 §2)")
    if kind == "board.post":
        if "author" not in ev:
            problems.append("게시물에 author가 없다 — 서명자(랩)와 저자(주민)는 "
                            "별도 필드다(주소 계약 §3 상속)")
        if not ev.get("board"):
            problems.append(
                "게시물에 board 좌표가 없다 — 게시물은 서명 시점에 board 좌표를 "
                "품고 태어난다. 재운반은 이 계약에 존재하지 않는다(계약 §3)")
    return problems


def project_board(events: list[dict[str, Any]]) -> dict[str, Any]:
    """append-only 스트림 → board 상태. 결정적이며, 계약 밖 이벤트는 지우지 않고
    rejected에 사유와 함께 남긴다(원장은 삭제가 없다 — 효력만 없다)."""
    state: dict[str, Any] = {
        "board": None, "status": None, "caretaker": None,
        "flags": dict(DEFAULT_FLAGS), "metadata": {},
        # 규모 다섯 축 — 부재는 '측정 안 함'이다. 숫자 기본값을 두지 않는다.
        "scale": {k: None for k in ("concurrent_seats", "registered", "active",
                                    "caretaker_capacity", "operating_window")},
        "members": set(), "join_requests": [], "posts": [], "notices": [],
        "rejected": [], "last_event_at": None,
    }
    for ev in events:
        state["last_event_at"] = ev.get("at", state["last_event_at"])
        problems = validate_event(ev)
        if problems:
            state["rejected"].append({"event": ev, "why": problems})
            continue
        kind = ev["kind"]
        if kind == "board.created":
            state["board"] = ev["board"]
            state["caretaker"] = ev.get("caretaker")
            state["status"] = "active"
        elif kind == "board.metadata":
            state["metadata"].update(
                {k: v for k, v in ev.items()
                 if k not in ("kind", "board", "scale", "flags", "at")})
            state["flags"].update(ev.get("flags") or {})
            for axis, obs in (ev.get("scale") or {}).items():
                if axis in state["scale"]:
                    state["scale"][axis] = obs
        elif kind == "board.member.admitted":
            state["members"].add((ev["member"]["lab"], ev["member"]["id"]))
        elif kind == "board.member.removed":
            state["members"].discard((ev["member"]["lab"], ev["member"]["id"]))
        elif kind == "board.join.requested":
            state["join_requests"].append(ev["member"])
        elif kind == "board.leave.requested":
            # 탈퇴 요청은 기록이고, 효력은 caretaker의 removed가 낸다(대칭).
            state["join_requests"] = [m for m in state["join_requests"]
                                      if (m["lab"], m["id"]) !=
                                         (ev["member"]["lab"], ev["member"]["id"])]
        elif kind in _STATUS_EVENTS:
            state["status"] = _STATUS_EVENTS[kind]
        elif kind == "board.post":
            author = (ev["author"]["lab"], ev["author"]["id"])
            if state["board"] is not None and ev["board"] != state["board"]:
                # §3의 의미층 강제 — wire에서는 h 태그가 서명 안이라 이 대조가
                # 암호학이 되지만, 의미층에서도 좌표 불일치는 효력 없음이다.
                state["rejected"].append(
                    {"event": ev, "why": [f"다른 board 좌표({ev['board']})로 "
                                          "서명된 게시물 — 재운반 금지(계약 §3)"]})
            elif state["status"] == "closed":
                state["rejected"].append(
                    {"event": ev, "why": ["폐쇄된 board는 새 게시물을 받지 않는다"
                                          " — fail-closed(계약 §4)"]})
            elif state["flags"].get("restricted") and author not in state["members"]:
                state["rejected"].append(
                    {"event": ev, "why": ["restricted board에 비멤버 게시 — "
                                          "입장은 기록된 결정이다(계약 §3)"]})
            else:
                state["posts"].append(ev)
        elif kind == "board.notice":
            # notice는 아무것도 지우지 않는다 — 채택은 수신자별 로컬 결정(§5).
            state["notices"].append(ev)
    return state


def threads(state: dict[str, Any]) -> dict[str, list[str]]:
    """스레드 투영: root post_id → 답글 post_id 목록(순서 보존)."""
    out: dict[str, list[str]] = {}
    parent_root: dict[str, str] = {}
    for p in state["posts"]:
        pid, reply_to = p["post_id"], p.get("reply_to")
        if reply_to is None:
            out[pid] = []
            parent_root[pid] = pid
        else:
            root = parent_root.get(reply_to, reply_to)
            out.setdefault(root, []).append(pid)
            parent_root[pid] = root
    return out


def adopted_view(state: dict[str, Any],
                 adopted_notices: set[str]) -> list[dict[str, Any]]:
    """notice 채택 후의 로컬 뷰 — 원장은 그대로고 뷰만 걸러진다(계약 §5)."""
    hidden = {n["target_post"] for n in state["notices"]
              if n["notice_id"] in adopted_notices}
    return [p for p in state["posts"] if p["post_id"] not in hidden]


def delivery_set(state: dict[str, Any],
                 carry: dict[str, bool]) -> set[tuple[str, str]]:
    """배달 대상 = 멤버 ∩ (자기 빌리지가 carry 중). carry는 board 원장 밖의
    빌리지 로컬 재량이다 — admission에 영향을 주지 않고 그 역도 같다(계약 §3)."""
    return {(lab, mid) for (lab, mid) in state["members"] if carry.get(lab)}


def compile_directory(streams: dict[str, list[dict[str, Any]]],
                      compiled_at: str) -> list[dict[str, Any]]:
    """directory = 주기 재컴파일(계약 §6). 등재 조건: caretaker 결속 + 폐쇄 아님.
    미등재는 존재 부정이 아니다 — 발견 가능성일 뿐이다."""
    rows = []
    for board_id, events in sorted(streams.items()):
        st = project_board(events)
        if st["caretaker"] is None or st["status"] == "closed":
            continue
        rows.append({"board": board_id, "status": st["status"],
                     "caretaker": st["caretaker"],
                     "operating_window": st["scale"]["operating_window"],
                     "last_seen": st["last_event_at"],
                     "compiled_at": compiled_at})
    return rows


def project_digest(events: list[dict[str, Any]], since: str | None = None
                   ) -> dict[str, Any]:
    """반가설 투영(계약 §8): 같은 스트림에서 '새로 온 것 묶음'을 파생한다.
    board 투영과 같은 수용 규칙을 쓴다 — 두 해의 비교가 같은 데이터 위에 선다."""
    st = project_board(events)
    new_posts = [p for p in st["posts"] if since is None or p.get("at", "") > since]
    th = threads(st)
    return {"since": since, "post_ids": [p["post_id"] for p in new_posts],
            "thread_count": len(th),
            "notice_ids": [n["notice_id"] for n in st["notices"]]}


# ── 전화번호부(profile·directory) ────────────────────────────────────────────

def _coord(s: dict[str, Any]) -> tuple[str, str, int]:
    """주소 계약 좌표 — epoch가 다르면 다른 주체다(조용한 합침 금지)."""
    return (s["lab"], s["id"], s.get("epoch", 1))


def _delegated_voice_ok(ev: dict[str, Any]) -> bool:
    """author≠subject의 유일한 합법 경로 — 위임 발화(acting_agent+delegation).
    은퇴 주민 철회·집합 주체(village/lab) 프로필을 caretaker가 낼 때."""
    return bool(ev.get("acting_agent")) and bool(ev.get("delegation"))


def validate_profile_event(ev: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    kind = ev.get("kind", "")
    subject = ev.get("subject")
    if not subject:
        return ["subject가 없다 — 전화번호부의 행은 주체 좌표 위에 선다(계약 §1)"]
    if kind in ("profile.announced", "profile.updated", "profile.withdrawn"):
        author = ev.get("author")
        if (not author or _coord(author) != _coord(subject)) \
                and not _delegated_voice_ok(ev):
            problems.append(
                "author≠subject인데 위임 발화 표기(acting_agent+delegation)가 "
                "없다 — 소개·철회는 주체의 발화이거나, 위임 근거가 기록된 "
                "대리 발화여야 한다(계약 §2)")
    if kind in ("profile.announced", "profile.updated"):
        profile = ev.get("profile") or {}
        extra = set(profile) - PROFILE_FIELDS
        if extra:
            problems.append(
                f"화이트리스트 밖 필드 {sorted(extra)} — 공개 선택된 최소만 "
                "싣는다. 원본은 빌리지에 남는다(계약 §3)")
        if profile.get("kind") not in SUBJECT_KINDS:
            problems.append("profile.kind는 creature|village|lab 중 하나여야 "
                            "한다(계약 §1)")
        if subject["lab"] != ev.get("signer_lab") and not ev.get("delegation"):
            problems.append(
                f"subject의 랩({subject['lab']})≠서명 랩({ev.get('signer_lab')})"
                "인데 위임 표기가 없다 — Maru 조항 상속(BBS 계약 §4a)")
    return problems


def project_profiles(events: list[dict[str, Any]],
                     voice_verifier: Callable[[dict[str, Any]], str] | None
                     = None) -> dict[str, Any]:
    """스트림 → 주체별 현재 프로필. updated는 대체, withdrawn은 대칭으로 내린다.

    **voice seam(Orin 037)**: voice_verifier(event)->grade("verified"|
    "unverifiable"|"rejected")를 주입하면 발화 체인을 등급 판정한다. 주입이 없으면
    voice 블록이 있어도 **unverifiable로 보존**(시도를 지우지 않는다)하고, verified는
    발행하지 않는다 — 기본 제품 경로는 claimed core다. verifier는 실험 경계
    (experiments/identity-v0)가 소유하며 core는 그것에 의존하지 않는다."""
    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    withdrawn: set[tuple[str, str, int]] = set()
    rejected: list[dict[str, Any]] = []
    for ev in events:
        problems = validate_profile_event(ev)
        if problems:
            rejected.append({"event": ev, "why": problems})
            continue
        evidence = None
        if ev.get("voice") is not None:
            if voice_verifier is None:
                evidence = "unverifiable"      # 시도 보존(부재≠거부)
            else:
                g = voice_verifier(ev)
                if g == "rejected":
                    rejected.append({"event": ev,
                                     "why": ["발화 체인 거부(주입된 verifier)"]})
                    continue
                evidence = g                   # "verified" | "unverifiable"
        coord = _coord(ev["subject"])
        kind = ev["kind"]
        if kind == "profile.announced" or kind == "profile.updated":
            prior = rows.get(coord)
            rows[coord] = {
                "subject": ev["subject"], "profile": ev["profile"],
                "vouch": ev.get("vouch"),
                "announced_at": prior["announced_at"] if prior else ev.get("at"),
                "last_seen": ev.get("at"), "voice_evidence": evidence,
            }
            withdrawn.discard(coord)
        elif kind == "profile.withdrawn":
            withdrawn.add(coord)
    return {"rows": rows, "withdrawn": withdrawn, "rejected": rejected}


def compile_people_directory(events: list[dict[str, Any]], compiled_at: str,
                             voice_verifier=None) -> list[dict[str, Any]]:
    """전화번호부 컴파일(§4) — 주기 재컴파일이 신선도를 강제한다(BBS §7 상속).

    행 스키마 고정: 활동·가용성 필드는 존재하지 않는다(등재≠활동·응답).
    subject_authority_verified는 verifier가 verified를 낸 행에만 붙고, voice
    시도가 있으나 미검증이면 voice_evidence로 상태를 보존한다(접지 않는다)."""
    st = project_profiles(events, voice_verifier)
    out = []
    for coord in sorted(st["rows"]):
        if coord in st["withdrawn"]:
            continue
        r = st["rows"][coord]
        row = {"subject": r["subject"], "profile": r["profile"],
               "vouch": r["vouch"], "subject_claimed": True,
               "announced_at": r["announced_at"],
               "profile_event_last_seen": r["last_seen"],
               "compiled_at": compiled_at}
        if r.get("voice_evidence") == "verified":
            row["subject_authority_verified"] = True
        if r.get("voice_evidence") is not None:
            row["voice_evidence"] = r["voice_evidence"]
        out.append(row)
    for row in out:
        base = {k for k in row
                if k not in ("subject_authority_verified", "voice_evidence")}
        assert base == set(_DIRECTORY_ROW_KEYS)
    return out


def public_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """문명 관측면(전망대 등) 투영 — contact는 항상 제거된다(수확 판례 §5).
    fail-closed: 산출물에 contact가 남으면 예외."""
    out = []
    for r in rows:
        p = {k: v for k, v in r["profile"].items() if k != "contact"}
        out.append({"subject": r["subject"], "profile": p,
                    "announced_at": r["announced_at"],
                    "profile_event_last_seen": r["profile_event_last_seen"]})
    for r in out:
        if "contact" in r["profile"]:
            raise AssertionError("public projection에 contact가 남았다 — "
                                 "수확 판례 위반(계약 §5)")
    return out
