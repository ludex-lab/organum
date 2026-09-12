"""organum bbs_wire — 게시판 클라이언트 층 (0.6.0, Orin 037 B 슬롯 · 040 계약).

0.5.0의 `organum.bbs`는 **순수 투영**(이벤트 배열 → 상태)이다. 케어테이커가 광장을
읽으려면 문마다 pull → verify-envelope → JSON 합치기 → at 정렬 → board 투영을 손으로
했다(09-04 plaza-004 스레드가 그 마찰을 셋 드러냈다). 이 모듈은 그 앞뒤를 제품으로
붙인다 — **발견·수거·검증·정렬**(앞) 과 **게시·재전송**(뒤). 서버는 그대로다(hunk 0).

층:
    bbs_cli(boards·pull·read·post) → bbs_wire → hub_drop(list_channels·fetch_page·
    pull_quads·push_quad) + hub_ops(build_message_envelope·sign_and_admit·export_quad·
    verify_quad) + bbs(project_board·threads·compile_people_directory)

계약(040):
- **서명자 결속(§2A)**: provenance는 검증된 envelope.signer에서만 온다. body의
  `signer_lab`이 있으면 대조하고 불일치는 배제·보고한다. 문(door) 이름은 증거가 아니다.
- **board 결속(§2B)**: `board.*` 모든 종류의 서명된 board 좌표를 요청 BOARD와 대조해
  다른 좌표는 reducer에 넘기지 않는다(다른 게시판의 closed가 이 게시판을 닫지 못한다).
- **한 봉투의 실패 격리(§2C)**: 서명·digest·shape 검사는 quad별이고, 문제 좌표를 남기며
  다른 정상 글은 계속 보인다. broad exception으로 내부 버그를 삼키지 않는다.
- **read는 오프라인·결정적(§3)**: 입력 = quad 트리 + hub registry 재생 + 회차 상태 +
  옵션. 현재 시각을 넣지 않고 원장을 갱신하지 않는다. `since`는 **전체 이력으로 상태를
  계산한 뒤** 표시만 좁힌다.
- **순서(§3)**: 표시/재생 순서는 `(at, lab, n)`. `at`은 문자열 비교(RFC3339 Z 권장) —
  같은 문 안에서 at이 역행하면 n 순서가 뒤집힌다(물리적 append-only·재생 순서·인과
  순서는 서로 다른 보장이다). `at` 부재·비문자열은 shape 실패로 배제한다.
- **발견(§4)**: 서버 channels 트리가 존재하는 문의 권위, 종류는 **내용의 관측**(서명
  검증 전이라 `inferred`), 구독은 로컬 선택. 확인 못 하면 unknown, 상충하면 ambiguous.
- **회차·재전송(§4)**: 회차 상태(`.round.json`)에 예정 문·성공/실패/미시도·페이지 수·
  마지막 완결 위치를 남긴다. post는 네트워크 전에 outbox에 quad를 확정하고, 응답 유실·
  timeout·5xx면 `unknown`과 재시도 좌표를 돌려준다. 재전송은 같은 번호·같은 bytes.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
from pathlib import Path
from typing import Any, Callable

try:
    from organum import bbs
    from organum import hub_drop as hd
    from organum import hub_ops as ho
except ImportError:                                    # 스크립트 직접 실행 경로
    import bbs
    import hub_drop as hd
    import hub_ops as ho

SORT_RULE = "(at, lab, n)"
ROUND_FILE = ".round.json"
ROUNDS_LOG = ".rounds.jsonl"
OUTBOX_SUFFIX = ".outbox.json"
MEDIA_JSON = "application/json"
BOARD_KINDS_PREFIX = "board."
PROFILE_KINDS_PREFIX = "profile."


class BbsWireError(ValueError):
    """구조화된 실패 — CLI가 문구를 stderr에 찍는다."""


# ── 트리·quad 파일 ───────────────────────────────────────────────────────────

def door_lab(door: str) -> str | None:
    return "lab:" + door[len("from-"):] if door.startswith("from-") else None


def event_body_bytes(event: dict[str, Any]) -> bytes:
    """의미 이벤트 → 정렬 JSON bytes. digest가 서명에 결속되는 그 bytes다."""
    return json.dumps(event, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def quad_numbers(door_dir: Path) -> list[str]:
    ns = set()
    for f in door_dir.iterdir():
        n = ho.quad_number(f.name)
        if n is not None and "-" in f.name:
            ns.add(f.name.split("-", 1)[0])
    return sorted(ns, key=int)


def quad_files(door_dir: Path, n: str) -> dict[str, Path | None]:
    env = door_dir / f"{n}-envelope.json"
    sig = door_dir / f"{n}-sig.txt"
    bodies = sorted(p for p in door_dir.glob(f"{n}-body.*") if p.is_file())
    return {"envelope": env if env.is_file() else None,
            "sig": sig if sig.is_file() else None,
            "body": bodies[0] if bodies else None}


def _envelope_status(env_bytes: bytes) -> tuple[str | None, bool]:
    """(미완성 사유 | None, body 결속 여부). JSON 아님·object 아님은 서로 다른 사유다.
    payload 타입 오류는 완결성 문제가 아니라 검증(schema) 문제라 여기서 죽지 않는다(041 R2)."""
    try:
        env = json.loads(env_bytes)
    except ValueError:
        return "envelope가 JSON이 아님", False
    if not isinstance(env, dict):
        return "envelope가 봉투 모양이 아님(object 아님)", False
    payload = env.get("payload")
    return None, (bool(payload.get("body_sha256")) if isinstance(payload, dict) else False)


def door_completeness(door_dir: Path) -> tuple[str | None, list[dict[str, Any]]]:
    """(마지막 완결 번호, 미완성 목록). 완결 = envelope+sig(+body, envelope가 body를
    결속할 때). envelope는 pull이 **마지막에** 쓰는 완결 표지다 — 있으면 앞 파일도 있어야
    하고, 없으면 미완성이다. 마지막 완결 = 첫 미완성 **앞**까지(뒤의 완결은 세지 않는다 —
    최고 번호만 보고 완료 취급하지 않기 위해, 040 §4)."""
    incomplete: list[dict[str, Any]] = []
    last: str | None = None
    if not door_dir.is_dir():
        return None, incomplete
    for n in quad_numbers(door_dir):
        f = quad_files(door_dir, n)
        why = None
        if f["envelope"] is None:
            why = "envelope 없음(미완성 quad — pull 중단 또는 손으로 바뀜)"
        elif f["sig"] is None:
            why = "sig 없음"
        else:
            bad, needs_body = _envelope_status(f["envelope"].read_bytes())
            if bad:
                why = bad
            elif needs_body and f["body"] is None:
                why = "envelope가 body를 결속하는데 body 파일이 없음"
        if why:
            incomplete.append({"n": n, "why": why})
        elif not incomplete:
            last = n
    return last, incomplete


def resume_point(door_dir: Path) -> str:
    """다음 pull의 `since` — 마지막 완결 번호(그 뒤부터 다시 받는다). 미완성이 있으면
    그 앞에서 재개해 서버가 다시 보내게 한다(pull은 기존 파일을 덮어쓰지 않고 빈 자리만
    채운다)."""
    last, _ = door_completeness(door_dir)
    return last or "000"


# ── 발견 ─────────────────────────────────────────────────────────────────────

def _kind_of_body(raw: bytes | None) -> str | None:
    """body 내용이 말하는 종류 — 'board' | 'directory' | 'other'(JSON이지만 계약 밖) |
    'text'(JSON 아님) | None(body 없음)."""
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return "text"
    if isinstance(obj, dict) and isinstance(obj.get("kind"), str):
        if obj["kind"].startswith(BOARD_KINDS_PREFIX):
            return "board"
        if obj["kind"].startswith(PROFILE_KINDS_PREFIX):
            return "directory"
    return "other"


def classify_channel(observations: list[tuple[str, str, str | None]]) -> dict[str, Any]:
    """관측 [(door, n, kind)] → {kind, basis}. 서명 검증 전이라 호출자가 inferred를 단다.
    board/directory 둘 다 보이면 ambiguous, 아무 계약 신호가 없으면 unknown — 봉투 레인
    으로 **확정하지 않는다**(040 §4: 초기 gap·후발 참여·미수거가 가능하다)."""
    seen = {k for _, _, k in observations if k in ("board", "directory")}
    basis = [{"door": d, "n": n, "kind": k} for d, n, k in observations
             if k in ("board", "directory")]
    if seen == {"board"}:
        kind = "board"
    elif seen == {"directory"}:
        kind = "directory"
    elif seen:
        kind = "ambiguous"
    else:
        kind = "unknown"
    return {"kind": kind, "basis": basis,
            "observed": len(observations),
            "non_contract": sum(1 for _, _, k in observations
                                if k in ("text", "other"))}


def discover(url_base: str, token: str, *, timeout: int = hd.CLIENT_TIMEOUT_SECONDS,
             warmup: bool = True, probe: bool = True,
             stats: dict | None = None) -> dict[str, Any]:
    """서버 트리 → 채널별 {doors, kind, inferred, basis}. probe=True면 문마다 첫 페이지
    한 GET(`stats["probe_gets"]`로 따로 계수 — 정상 증분 수거의 ≈18과 섞지 않는다)."""
    st = stats if stats is not None else {}
    tree = hd.list_channels(f"{url_base}/v0/channels", token, timeout=timeout,
                            warmup=warmup, stats=st)["channels"]
    out: dict[str, Any] = {}
    for ch in sorted(tree):
        doors = sorted(tree[ch])
        obs: list[tuple[str, str, str | None]] = []
        errors: list[dict[str, str]] = []
        if probe:
            for door in doors:
                try:
                    page = hd.fetch_page(f"{url_base}/v0/{ch}/{door}", token, "000",
                                         timeout=timeout)
                except (hd.DropError, urllib.error.URLError, OSError) as e:
                    errors.append({"door": door, "error": f"{type(e).__name__}: {e}"})
                    continue
                st["probe_gets"] = st.get("probe_gets", 0) + 1
                for q in page.get("quads", []):
                    raw = (base64.b64decode(q["body_b64"])
                           if q.get("body_b64") else None)
                    obs.append((door, q.get("n", "?"), _kind_of_body(raw)))
        info = classify_channel(obs)
        info.update({"doors": doors, "inferred": True, "probe_errors": errors})
        if not probe:
            info["kind"] = "unknown"
        out[ch] = info
    return out


# ── 수거 ─────────────────────────────────────────────────────────────────────

def _write_round(ch_dir: Path, rnd: dict[str, Any]) -> None:
    """회차 파일은 임시 파일 + 원자 교체로 게시한다(중단돼도 반쯤 쓰인 JSON이 남지 않는다)."""
    data = (json.dumps(rnd, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    tmp = ch_dir / (ROUND_FILE + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, ch_dir / ROUND_FILE)


def pull_channel(url_base: str, token: str, channel: str, tree_root: str | Path, *,
                 doors: list[str] | None = None,
                 timeout: int = hd.CLIENT_TIMEOUT_SECONDS, warmup: bool = True,
                 round_at: str | None = None, stats: dict | None = None
                 ) -> dict[str, Any]:
    """문 전부 pull(회차 워밍 1회·문별 no-warmup). 한 문의 실패가 다른 문을 막지 않는다.

    회차 상태(`<tree>/<channel>/.round.json`)는 **첫 문 호출 전에** `running`으로 게시하고
    문마다 갱신하며 끝에 `complete`로 닫는다(여울 085 §2 — 중단된 회차도 계획·완료 위치를
    남긴다). 완료된 회차만 `.rounds.jsonl`에 한 줄 덧붙인다. 문 결과의 `ok`는 pull 성공
    **그리고** 미완성 quad 0일 때만 참이다(Ray 101 F1 — 복구가 안 된 회차는 성공으로 닫지
    않는다). `pages`는 정상 응답 페이지 수이지 총 HTTP 시도 수가 아니다.

    `round_at`은 호출자가 준다(read의 결정성 입력 — 이 함수가 시각을 지어내지 않는다)."""
    st = stats if stats is not None else {}
    ch_dir = Path(tree_root) / channel
    ch_dir.mkdir(parents=True, exist_ok=True)
    rnd: dict[str, Any] = {"channel": channel, "round_at": round_at, "status": "running",
                           "planned": None, "results": {}, "untried": [],
                           "pages_total": 0, "timeout_s": timeout, "warmup": warmup,
                           "warm_ms": None, "warm_ok": None, "warm_budget_s": None,
                           "error": None}
    if doors is None:
        try:
            tree = hd.list_channels(f"{url_base}/v0/channels", token, timeout=timeout,
                                    warmup=warmup, stats=st)["channels"]
        except (hd.DropError, urllib.error.URLError, OSError, ValueError) as e:
            rnd.update({"status": "discovery-failed",
                        "error": f"{type(e).__name__}: {e}"})
            _write_round(ch_dir, rnd)
            raise BbsWireError(f"채널 발견 실패 — 예정 문 미확정: {e}")
        doors = sorted(tree.get(channel) or [])
        warmup = False                         # channels 호출이 이미 덥혔다
        for k in ("warm_ms", "warm_ok", "warm_budget_s"):   # 발견이 덥힌 계측(LxM 069 §3-2)
            if k in st:
                rnd[k] = st[k]
    rnd["planned"] = doors
    rnd["untried"] = list(doors)
    _write_round(ch_dir, rnd)
    first = True
    for door in doors:
        ddir = ch_dir / door
        since = resume_point(ddir)
        dst: dict = {}
        entry: dict[str, Any] = {"since": since}
        try:
            ns = hd.pull_quads(f"{url_base}/v0/{channel}/{door}", token, ddir,
                               since=since, timeout=timeout,
                               warmup=(warmup and first), stats=dst)
            entry.update({"ok": True, "pulled": ns})
        except (hd.DropError, urllib.error.URLError, OSError, ValueError) as e:
            entry.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        first = False
        last, incomplete = door_completeness(ddir)
        entry.update({"pages": dst.get("pages", 0), "repaired": dst.get("repaired", 0),
                      "last_complete": last, "incomplete": incomplete})
        if incomplete and entry["ok"]:
            entry["ok"] = False
            entry["error"] = ("미완성 quad 남음(재수거로 복구되지 않음): "
                              + ", ".join(i["n"] for i in incomplete))
        for k in ("warm_ms", "warm_ok", "warm_budget_s"):
            if k in dst and rnd[k] is None:
                rnd[k] = dst[k]
                st.setdefault(k, dst[k])
        rnd["results"][door] = entry
        rnd["untried"] = [d for d in doors if d not in rnd["results"]]
        rnd["pages_total"] = sum(r.get("pages", 0) for r in rnd["results"].values())
        _write_round(ch_dir, rnd)
    rnd["status"] = "complete"
    _write_round(ch_dir, rnd)
    with (ch_dir / ROUNDS_LOG).open("ab") as f:
        f.write((json.dumps(rnd, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return rnd


def completeness_of(rnd: dict[str, Any] | None, problems: list) -> str:
    """읽기 범위의 완결성(041 R4): complete(닫힌 회차·문제 0) · partial(실패·미시도·미완성·
    닫히지 않음) · **unknown**(회차 기록 자체가 없음 — 외부 수거기가 만든 미러 등; 부분인지
    전체인지 이 트리만으로는 말할 수 없다)."""
    if rnd is None:
        return "unknown"
    return "partial" if problems else "complete"


def round_problems(rnd: dict[str, Any] | None) -> list[dict[str, Any]]:
    """회차 상태에서 '부분 갱신'을 읽어 낸다(여울 085 §1) — 실패한 문·미시도 문·미완성 quad·
    닫히지 않은(running/discovery-failed) 회차. read는 파일 검증 문제와 별도로 이것을 병기하고
    CLI는 둘 중 하나라도 있으면 exit 1이다."""
    if not rnd:
        return []
    out: list[dict[str, Any]] = []
    status = rnd.get("status")
    if status in ("running", "discovery-failed"):
        out.append({"door": None, "why": f"회차가 닫히지 않음(status={status})"
                                          + (f": {rnd.get('error')}" if rnd.get("error") else "")})
    for door, r in sorted((rnd.get("results") or {}).items()):
        if not r.get("ok"):
            out.append({"door": door, "why": f"수거 실패: {r.get('error')}"})
    for door in rnd.get("untried") or []:
        out.append({"door": door, "why": "미시도(회차가 거기 닿기 전에 끝남)"})
    return out


def load_round(tree_root: str | Path, channel: str) -> dict[str, Any] | None:
    p = Path(tree_root) / channel / ROUND_FILE
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_bytes().decode("utf-8"))
    except ValueError:
        return {"error": "회차 상태 파일이 JSON이 아님"}


# ── 적재·검증 ────────────────────────────────────────────────────────────────

def _shape_problems(ev: Any, expect: str, channel: str) -> list[str]:
    """인입 shape = core의 kind별 구조 검사(bbs.shape_problems, 041 R2) + 채널 결속."""
    out = list(bbs.shape_problems(ev))
    if out:
        return out
    kind = ev["kind"]
    if expect == "board":
        if not kind.startswith(BOARD_KINDS_PREFIX):
            out.append(f"게시판 채널에 board.* 아닌 이벤트({kind})")
        elif ev.get("board") != channel:
            out.append(f"서명된 board 좌표({ev.get('board')!r})가 요청 게시판"
                       f"({channel!r})과 다름 — 어떤 종류든 reducer에 넘기지 않는다"
                       "(040 §2B)")
    elif expect == "directory":
        if not kind.startswith(PROFILE_KINDS_PREFIX):
            out.append(f"전화번호부 채널에 profile.* 아닌 이벤트({kind})")
    return out


def load_channel(tree_root: str | Path, channel: str, hub, *, expect: str = "board",
                 doors: list[str] | None = None) -> dict[str, Any]:
    """quad 트리 → 검증된 이벤트(정렬) + 문제 목록. 원장 무접촉(verify_quad).

    반환: {"events": [ev,…], "records": [{door,n,event_id,signer,body_sha256,at,
    key_valid_from_seq,key_revoked_at_seq}], "problems": [{door,n,why}], "doors": [...]}
    events[i] ↔ records[i]. 이벤트 dict는 원문에 `signer_lab`(검증된 서명자)만 더한 사본."""
    ch_dir = Path(tree_root) / channel
    problems: list[dict[str, Any]] = []
    triples: list[tuple[tuple[str, str, int], dict, dict]] = []
    door_names = (sorted(doors) if doors is not None else
                  sorted(p.name for p in ch_dir.glob("from-*") if p.is_dir())
                  if ch_dir.is_dir() else [])
    for door in door_names:
        ddir = ch_dir / door
        if not ddir.is_dir():
            problems.append({"door": door, "n": None, "why": "문 디렉터리 없음"})
            continue
        _, incomplete = door_completeness(ddir)
        for inc in incomplete:
            problems.append({"door": door, "n": inc["n"], "why": inc["why"]})
        skip = {inc["n"] for inc in incomplete}
        for n in quad_numbers(ddir):
            if n in skip:
                continue
            f = quad_files(ddir, n)
            try:
                env = json.loads(f["envelope"].read_bytes().decode("utf-8"))
                sig = f["sig"].read_bytes().decode("utf-8").strip()
            except (ValueError, UnicodeDecodeError) as e:
                problems.append({"door": door, "n": n,
                                 "why": f"envelope/sig 파일 파싱 실패: {e}"})
                continue
            if not isinstance(env, dict) or not isinstance(env.get("signer"), dict):
                problems.append({"door": door, "n": n,
                                 "why": "envelope가 봉투 모양이 아님(object·signer 필요)"})
                continue
            body = f["body"].read_bytes() if f["body"] is not None else None
            try:
                v = ho.verify_quad(env, sig, hub=hub, body=body)
            except ho.HubOpsError as e:
                problems.append({"door": door, "n": n, "why": f"검증 불가: {e}"})
                continue
            if not v["ok"]:
                why = []
                if not v["valid_signature"]:
                    why.append("서명 무효")
                if v["schema_problems"]:
                    why.append(f"봉투 스키마 위반: {v['schema_problems']}")
                if v["body_sha256_match"] is False:
                    why.append("body digest 불일치")
                problems.append({"door": door, "n": n, "why": "; ".join(why)})
                continue
            signer_id = (v.get("signer") or {}).get("id")
            if body is None:
                problems.append({"door": door, "n": n, "why": "body 없음"})
                continue
            try:
                ev = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                problems.append({"door": door, "n": n, "why": "body가 JSON이 아님"})
                continue
            shape = _shape_problems(ev, expect, channel)
            if shape:
                problems.append({"door": door, "n": n, "why": "; ".join(shape)})
                continue
            declared = ev.get("signer_lab")
            if declared is not None and declared != signer_id:
                problems.append({"door": door, "n": n,
                                 "why": f"body.signer_lab({declared!r})가 검증된 서명자"
                                        f"({signer_id!r})와 다름 — 배제(040 §2A)"})
                continue
            ev = dict(ev)
            ev["signer_lab"] = signer_id
            if door_lab(door) != signer_id:
                problems.append({"door": door, "n": n,
                                 "why": f"문({door})과 서명자({signer_id})가 다름 — "
                                        "문 이름은 증거가 아니라 배제하지는 않되 기록"})
            rec = {"door": door, "n": n, "event_id": v["event_id"], "signer": signer_id,
                   "body_sha256": ((env.get("payload") or {}).get("body_sha256")),
                   "at": ev["at"],
                   "key_valid_from_seq": v.get("key_valid_from_seq"),
                   "key_revoked_at_seq": v.get("key_revoked_at_seq")}
            triples.append(((ev["at"], signer_id, int(n)), ev, rec))
    triples.sort(key=lambda t: t[0])
    return {"events": [ev for _, ev, _ in triples],
            "records": [rec for _, _, rec in triples],
            "problems": problems, "doors": door_names, "sort_rule": SORT_RULE}


# ── 읽기 ─────────────────────────────────────────────────────────────────────

def _sort_key(rec: dict[str, Any]) -> list[Any]:
    return [rec["at"], rec["signer"], int(rec["n"])]


def read_board(tree_root: str | Path, board: str, hub, *, since: str | None = None,
               after: str | None = None, doors: list[str] | None = None,
               voice_verifier: Callable | None = None) -> dict[str, Any]:
    """오프라인·결정적 읽기. 전체 확보 이력으로 상태를 계산한 뒤 표시만 좁힌다.

    `since`: at > since인 게시물만 표시. `after`: "lab:x:n" — 그 quad 뒤(정렬 순)의
    게시물만 표시. 둘 다 상태(멤버·폐쇄·스레드 부모)에는 손대지 않는다."""
    loaded = load_channel(tree_root, board, hub, expect="board", doors=doors)
    rec_of = {id(ev): rec for ev, rec in zip(loaded["events"], loaded["records"])}
    state = bbs.project_board(loaded["events"])
    th = bbs.threads(state)

    def enrich(ev: dict[str, Any]) -> dict[str, Any]:
        rec = rec_of.get(id(ev))
        out = dict(ev)
        if rec is not None:
            out["provenance"] = {k: rec[k] for k in ("door", "n", "event_id", "signer",
                                                     "body_sha256")}
            out["sort_key"] = _sort_key(rec)
        return out

    posts = [enrich(p) for p in state["posts"]]
    cursor = None
    if after:
        parts = after.rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            raise BbsWireError(f"--after는 lab:<name>:<n> 꼴: {after!r}")
        target = next((r for r in loaded["records"]
                       if r["signer"] == parts[0] and int(r["n"]) == int(parts[1])), None)
        if target is None:
            raise BbsWireError(f"--after 좌표를 트리에서 찾지 못함: {after!r}")
        cursor = _sort_key(target)
    shown = [p for p in posts
             if (since is None or p.get("at", "") > since)
             and (cursor is None or p["sort_key"] > cursor)]
    rnd = load_round(tree_root, board)
    rp = round_problems(rnd)
    completeness = completeness_of(rnd, rp)
    rejected = []
    for r in state["rejected"]:
        ev = r["event"]
        rec = rec_of.get(id(ev))
        rejected.append({"why": r["why"], "kind": ev.get("kind"),
                         "post_id": ev.get("post_id"), "at": ev.get("at"),
                         **({"provenance": {k: rec[k] for k in ("door", "n", "event_id",
                                                                "signer")}}
                            if rec else {})})
    return {
        "board": board, "status": state["status"], "caretaker": state["caretaker"],
        "flags": state["flags"], "metadata": state["metadata"], "scale": state["scale"],
        "members": sorted([list(m) for m in state["members"]]),
        "join_requests": state["join_requests"],
        "posts": shown, "post_count_total": len(posts),
        "notices": [enrich(n) for n in state["notices"]],
        "threads": th,
        "rejected": rejected, "rejected_count": len(rejected),
        "transport_problems": loaded["problems"],
        "transport_problem_count": len(loaded["problems"]),
        "doors_read": loaded["doors"],
        "round": rnd,
        "round_problems": rp, "round_problem_count": len(rp),
        "completeness": completeness,
        "sort_rule": SORT_RULE,
        "display_filter": {"since": since, "after": after},
        "last_event_at": state["last_event_at"],
    }


def read_directory(tree_root: str | Path, channel: str, hub, *, compiled_at: str,
                   doors: list[str] | None = None,
                   voice_verifier: Callable | None = None) -> dict[str, Any]:
    """전화번호부 채널 읽기 — compile_people_directory(계약 v0.2). compiled_at은 호출자가
    준다(결정성 — read가 시각을 지어내지 않는다)."""
    loaded = load_channel(tree_root, channel, hub, expect="directory", doors=doors)
    rows = bbs.compile_people_directory(loaded["events"], compiled_at, voice_verifier)
    st = bbs.project_profiles(loaded["events"], voice_verifier)
    rec_of = {id(ev): rec for ev, rec in zip(loaded["events"], loaded["records"])}
    rejected = [{"why": r["why"], "kind": r["event"].get("kind"),
                 "subject": r["event"].get("subject"),
                 **({"provenance": {k: rec_of[id(r['event'])][k]
                                    for k in ("door", "n", "event_id", "signer")}}
                    if id(r["event"]) in rec_of else {})}
                for r in st["rejected"]]
    rnd = load_round(tree_root, channel)
    rp = round_problems(rnd)
    return {"channel": channel, "compiled_at": compiled_at, "rows": rows,
            "completeness": completeness_of(rnd, rp),
            "row_count": len(rows), "rejected": rejected,
            "rejected_count": len(rejected),
            "transport_problems": loaded["problems"],
            "transport_problem_count": len(loaded["problems"]),
            "doors_read": loaded["doors"], "round": rnd,
            "round_problems": rp, "round_problem_count": len(rp),
            "sort_rule": SORT_RULE}


# ── 게시(durable outbox) ─────────────────────────────────────────────────────

def outbox_dir(root: str | Path, channel: str) -> Path:
    """outbox는 **랩당 루트 하나**, 채널별 `out-<channel>/` 하위(hub CLI export 관례와 같은
    이름 — 기존 `out-bbs-plaza/` 번호가 그대로 이어진다). 문 번호는 서버와 outbox가 함께
    세는 것이라 같은 문에 outbox 두 벌을 쓰면 409로 막힌다(자동 우회 없음, 040 §4)."""
    return Path(root) / f"out-{channel}"


def _outbox_record_path(outbox: Path, n: str) -> Path:
    return outbox / f"{n}{OUTBOX_SUFFIX}"


def _find_outbox_by_event(outbox: Path, event_id: str) -> dict[str, Any] | None:
    if not outbox.is_dir():
        return None
    for p in sorted(outbox.glob(f"*{OUTBOX_SUFFIX}")):
        try:
            rec = json.loads(p.read_bytes().decode("utf-8"))
        except ValueError:
            continue
        if rec.get("event_id") == event_id:
            return rec
    return None


def _save_outbox(outbox: Path, rec: dict[str, Any]) -> None:
    _outbox_record_path(outbox, rec["n"]).write_bytes(
        (json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def prepare_post(*, hub_dir, seed_path, signer: str, key_id: str, epoch: int,
                 board: str, event: dict[str, Any], url_base: str,
                 to_lab: str, to_id: str = "board", to_epoch: int = 1,
                 outbox: str | Path, created_at: str | None = None) -> dict[str, Any]:
    """네트워크 전 단계 전부: 의미 검증 → 봉투 빌드 → 서명·자기 원장 admit → outbox에
    quad 확정(번호·bytes·대상 문). 같은 이벤트 재호출은 같은 quad로 수렴한다."""
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
        raise BbsWireError("이벤트는 kind를 가진 JSON object여야 한다")
    ev = dict(event)
    if ev["kind"].startswith(BOARD_KINDS_PREFIX) and ev.get("board") != board:
        raise BbsWireError(f"이벤트 board 좌표({ev.get('board')!r})가 게시판({board!r})과 "
                           "다르다 — 좌표는 서명 시점에 품고 태어난다(계약 §3)")
    declared = ev.setdefault("signer_lab", signer)
    if declared != signer:
        raise BbsWireError(f"이벤트 signer_lab({declared!r})이 서명자({signer!r})와 다르다")
    shape = bbs.shape_problems(ev)
    if shape:
        raise BbsWireError("구조 위반이라 서명하지 않는다(041 R2): " + " / ".join(shape))
    semantic = bbs.validate_event(ev) if ev["kind"].startswith(BOARD_KINDS_PREFIX) \
        else bbs.validate_profile_event(ev)
    if semantic:
        raise BbsWireError("계약 위반이라 서명하지 않는다: " + " / ".join(semantic))
    door = hd._door_for_signer(signer)
    if door is None:
        raise BbsWireError(f"서명자 {signer!r}에서 문 이름을 파생할 수 없다(fail-closed)")
    outbox = outbox_dir(outbox, board)
    outbox.mkdir(parents=True, exist_ok=True)
    body = event_body_bytes(ev)
    seed = ho.read_seed(seed_path)
    locator = f"file://{board}-{ev.get('post_id') or ev['kind']}.json"
    # 041 R1: load/replay → idempotency → append → outbox 조회·배정이 **한 락** 아래.
    with ho.hub_writer(hub_dir) as (d, cfg, hub):
        env = ho.build_message_envelope(cfg, signer=signer, key_id=key_id, epoch=epoch,
                                        to_lab=to_lab, to_id=to_id, to_epoch=to_epoch,
                                        body=body, body_locator=locator,
                                        media_type=MEDIA_JSON, created_at=created_at)
        r = ho.sign_and_admit(d, cfg, hub, env, seed)
        if not r["admitted"]:
            raise BbsWireError(f"자기 원장 admit 거부({r.get('stage')}): {r.get('problems')}")
        existing = _find_outbox_by_event(outbox, r["event_id"])
        if existing is not None:
            return existing
        tmp = outbox / f".body-{r['event_id'][:16]}.json"
        tmp.write_bytes(body)
        try:
            exp = ho.export_quad(d, outbox, event_id=r["event_id"], body_path=tmp)
        finally:
            tmp.unlink(missing_ok=True)
        rec = {"n": exp["nnn"], "event_id": r["event_id"], "board": board,
               "door_url": f"{url_base}/v0/{board}/{door}", "signer": signer,
               "body_sha256": env["payload"]["body_sha256"], "outbox": str(outbox),
               "status": "pending", "attempts": [], "dedup": None}
        _save_outbox(outbox, rec)
        return rec


def push_outbox(outbox: str | Path, channel: str, n: str, token: str, *,
                timeout: int = hd.CLIENT_TIMEOUT_SECONDS, warmup: bool = True,
                attempt_at: str | None = None, stats: dict | None = None
                ) -> dict[str, Any]:
    """outbox(루트/out-<channel>)의 quad n을 같은 번호·같은 bytes로 push. 결과 status:
    stored(저장, dedup 여부 병기) · unknown(응답 유실·timeout·5xx — 재시도 좌표 반환) ·
    conflict(409 같은 번호 다른 bytes — 자동 우회 없음) · rejected(4xx). 저장 성공은
    게시판 멤버십 ACCEPT와 다른 결과다 — 그건 read의 투영이 판정한다."""
    outbox = outbox_dir(outbox, channel)
    rp = _outbox_record_path(outbox, n)
    if not rp.is_file():
        raise BbsWireError(f"outbox 기록 없음: {rp}")
    rec = json.loads(rp.read_bytes().decode("utf-8"))
    st = stats if stats is not None else {}
    attempt: dict[str, Any] = {"at": attempt_at}
    try:
        r = hd.push_quad(rec["door_url"], token, outbox / n, timeout=timeout,
                         warmup=warmup, stats=st)
        rec["status"] = "stored"
        rec["dedup"] = bool(r.get("dedup"))
        attempt["result"] = "stored"
    except hd.DropError as e:
        attempt["result"] = f"HTTP {e.status}"
        if e.status == 409:
            rec["status"] = "conflict"
        elif 500 <= e.status < 600:
            rec["status"] = "unknown"
        else:
            rec["status"] = "rejected"
        attempt["error"] = str(e)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        rec["status"] = "unknown"
        attempt["result"] = f"{type(e).__name__}"
        attempt["error"] = str(e)
    rec["attempts"].append(attempt)
    _save_outbox(outbox, rec)
    out = {"n": rec["n"], "event_id": rec["event_id"], "status": rec["status"],
           "dedup": rec["dedup"], "door_url": rec["door_url"],
           "attempts": len(rec["attempts"])}
    if rec["status"] == "unknown":
        out["retry"] = {"outbox": str(outbox), "n": rec["n"],
                        "how": "organum-bbs post --retry N — 같은 번호·같은 bytes 재push"
                               "(서버 dedup 멱등). 새 message를 만들지 않는다"}
    if attempt.get("error"):
        out["error"] = attempt["error"]
    out.update({k: st[k] for k in ("warm_ms", "warm_ok", "warm_budget_s") if k in st})
    return out
