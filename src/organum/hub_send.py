"""hub send — 서명하는 쪽이 도는 차례(0.10.0). 승인된 초안 하나를 편지 하나로 만든다.

한 연구소의 발신을 서버 쪽이 맡는 예외의 뒤쪽 절반이다(설계: docs/hub-mail-send-v0-design.md).
모델이 부를 수 있는 표면이 없다. 사람이 승인 화면에서 부르고, 호스팅의 서비스가 돈다. 서명 키,
그 연구소의 원장과 발신함, 드롭의 쓰기 토큰이 여기에 온다.

    open_ledger(…)                 서명하기 전에 원장을 연다. 빈 자리면 상태 칸에서 되살리고, 발신함을
                                   문과 맞댄다. 디스크가 없는 배치는 뜰 때마다 부른다.
    review(store, draft_id, …)     승인 화면이 **보여야 하는 것**: 받는 이, 지문, 그리고 받은 바이트를
                                   꾸미지 않은 글. 보이지 않는 글자와 방향을 바꾸는 글자를 드러낸다.
    decide(store, draft_id, …)     승인이나 거절을 적는다. 승인은 그 사람이 본 지문에 묶인다.
    send_approved(store, …)        승인된 초안을 서명해 문에 올리고 되읽는다.
    resume(store, …)               승인됐고 결과가 없는 초안을 찾아 잇는다. 뜰 때와 화면이 열릴 때 부른다.
    drain(store, …)                서명됐으나 아직 문에 없는 편지를 문에 보낸다.

지키는 것:

1. **서명하는 것은 승인된 `message.posted`뿐이다.** 다른 종류의 봉투를 짓는 길이 이 모듈에 없다.
2. **승인은 받은 바이트에 묶인다.** 서명 직전에 함의 바이트를 다시 재어, 승인한 지문과 같을 때만
   서명한다. 승인 하나에 편지 하나다.
3. **같은 초안은 같은 편지로 모인다.** 봉투의 `body_locator`가 `draft:<draft_id>`이고 원장의 중복
   키가 내용에서 나온다. 다시 해도 처음의 봉투, 서명, 번호를 쓴다. 새 봉투를 짓지 않는다.
4. **번호를 쓴 편지는 문에 간다.** 서명된 편지가 문에 가기 전에는 다음 편지에 번호를 주지 않는다.
   문에 빈 번호를 남기지 않는다.
5. **`checkpoint`가 올리기보다 앞이다.** 표지를 보기 전에는 올리지 않는다(0.8.0의 차례).
6. **`sent`는 문에서 되읽은 때다.** 표지를 쓰는 배치에서는 그 번호가 바깥에 옮겨진 것까지 본다.
7. 기록(결정, 결과)을 쓰지 못하면 그 걸음은 끝나지 않은 것이다. 다음에 다시 온다.
8. **함에서 읽은 초안을 믿지 않는다.** 초안 창구가 본 것을 여기서 다시 본다 — 이름이 내용에서 나온
   그 이름인가, 받는 연구소와 그 epoch가 적어 둔 것인가, 크기와 꼴이 한도 안인가. 창구의 계정이
   새어 한도 밖의 초안이 놓여도 서명되지 않는다(LxM 184 §2).
9. **원장을 열기 전에는 서명하지 않는다.** 되살린 발신함이 문과 어긋나면 그 자리에서는 서명하지 않는다.
10. **원장에 선 편지가 발신함보다 먼저다.** 서명해 원장에 넣고 발신함에 놓기 전에 죽은 편지가 있으면,
    어느 초안을 부르든 그 편지부터 발신함에 놓는다 — 처음의 봉투와 서명 그대로, 함의 본문을 지문으로
    맞대어. 그 전에는 다른 초안에 봉투도 번호도 주지 않고 체크포인트도 뜨지 않는다(Orin 062 R1).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path

from . import hub_draft as hdr
from . import hub_drop as hd
from . import hub_envelope as he
from . import hub_ops as ho
from . import hub_state as hs

APPROVAL_TTL_SECONDS = 86_400           # 승인하고 이만큼 안에 서명하지 못하면 승인이 끝난다
RESUME_SCAN = 200                       # 잇기가 한 번에 받는 승인된 초안의 수
MIRROR_WAIT_SECONDS = 30.0              # 올린 편지가 바깥에 옮겨지기를 기다리는 시간(표지를 쓰는 배치)
SEND_LOCK = ".send.lock"                # 발신의 차례 전체를 감싸는 락. 원장의 락과 이름이 다르다
_SUFFIX = {"text/markdown": ".md", "text/plain": ".txt"}


class SendStopped(Exception):
    """사람이 봐야 하는 멈춤. 문과 발신함이 어긋났거나, 체크포인트가 멈췄거나, 원장이 받지 않았다.
    스스로 풀려고 다시 서명하거나 다시 번호를 주지 않는다."""


class Refused(Exception):
    """이 초안은 보내지 않는다. 번호를 쓰지 않았다."""


class _Later(Exception):
    """드롭이 지금 답하지 못한다(잠듦, 빈도 한도, 끊김). 멈춤이 아니다 — 조금 뒤에 다시 온다."""


# ── 승인 화면이 보여야 하는 것 ───────────────────────────────────────────────

def shown(body: bytes) -> dict:
    """받은 바이트 → 사람에게 보일 글. 꾸미지 않는다(마크다운도 HTML도 그리지 않는다). 줄바꿈과 탭은
    그대로 두고, 그 밖의 보이지 않는 글자(제어 문자, 폭이 없는 글자, 방향을 바꾸는 글자, 꼬리표 글자)는
    `⟦U+XXXX⟧`로 드러낸다. 눈에 보인 것과 서명되는 것이 달라질 틈을 줄인다."""
    try:
        text = body.decode("utf-8")                   # 초안은 UTF-8로만 받는다
    except UnicodeDecodeError:
        raise Refused("the stored draft is not UTF-8 text") from None
    out, revealed = [], []
    for i, ch in enumerate(text):
        if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"):
            out.append(ch)
            continue
        code = f"U+{ord(ch):04X}"
        out.append(f"⟦{code}⟧")
        revealed.append({"at": i, "codepoint": code, "name": unicodedata.name(ch, "")})
    return {"text": "".join(out), "revealed": revealed}


def _checked(draft_id: str, entry: dict, body: bytes, *, signer: str, recipients: dict,
             body_max_bytes: int) -> dict:
    """함에서 읽은 초안이 서명해도 되는 초안인가. 아니면 `Refused`다. 초안 창구가 받을 때 본 것을
    여기서 다시 본다 — 창구가 뚫렸거나 그 계정이 샜을 때 함에 무엇이 놓였을지 모른다."""
    draft = entry["draft"]
    try:
        to = draft["to"]
        want = hdr.draft_id(sender=draft["sender"], to_lab=to["lab_id"], to_id=to["to_id"],
                            media_type=draft["media_type"], body_sha256=draft["body_sha256"],
                            client_ref=draft["client_ref"])
        if not (draft_id == draft["draft_id"] == want):
            raise Refused("the draft's name does not come from its content")
        if draft["sender"] != signer:
            raise Refused("the draft belongs to another sender")
        if not (isinstance(to["lab_id"], str) and to["lab_id"] in recipients
                and to["to_epoch"] == recipients[to["lab_id"]]):
            raise Refused("the recipient is not allowed for this sender")
        hdr.check_to_id(to["to_id"])
        hdr.check_media_type(draft["media_type"])
        hdr.check_client_ref(draft["client_ref"])
        hdr.check_body(body, body_max_bytes)
        if hashlib.sha256(body).hexdigest() != draft["body_sha256"] or len(body) != draft["body_size"]:
            raise Refused("the stored draft does not match its record")
    except (KeyError, TypeError):
        raise Refused("the draft record is malformed") from None
    except ValueError as e:
        raise Refused(str(e)) from None
    return draft


def review(store, draft_id: str, *, signer: str, recipients: dict, now=time.time,
           body_max_bytes: int = hdr.DRAFT_BODY_MAX_BYTES,
           draft_ttl: float = hdr.DRAFT_TTL_SECONDS) -> dict:
    """승인 화면에 낼 것 전부. 서명해도 되는 초안이 아니면 내지 않는다(`Refused`) — 그런 초안은
    거절만 할 수 있다."""
    entry = store.get(draft_id)
    body = store.body(draft_id)
    draft = _checked(draft_id, entry, body, signer=signer, recipients=recipients,
                     body_max_bytes=body_max_bytes)
    return {"draft_id": draft_id, "status": hdr.status_of(entry, now(), draft_ttl),
            "sender": draft["sender"], "to": dict(draft["to"]), "media_type": draft["media_type"],
            "client_ref": draft["client_ref"], "created_at": draft["created_at"],
            "placed_at": entry.get("placed_at"), "expires_at": draft["expires_at"],
            "body_size": draft["body_size"], "body_sha256": draft["body_sha256"], **shown(body)}


def decide(store, draft_id: str, decision: str, *, approver: str, signer: str, recipients: dict,
           body_sha256: str | None = None, now=time.time, ttl: float = APPROVAL_TTL_SECONDS,
           body_max_bytes: int = hdr.DRAFT_BODY_MAX_BYTES,
           draft_ttl: float = hdr.DRAFT_TTL_SECONDS) -> dict:
    """승인(`approved`)이나 거절(`rejected`)을 적는다. 첫 결정이 남는다. 같은 결정을 다시 적으면
    그대로 돌아오고, 다른 결정을 덮어 적지 못한다.

    승인에는 `body_sha256`이 든다. 그 사람이 화면에서 본 글의 지문이다 — 함의 바이트가 그것과 다르거나
    서명해도 되는 초안이 아니면 적지 않는다. 거절은 본문을 읽지 않는다. 꼴이 틀린 초안도 거절할 수
    있어야 화면이 비워진다."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision은 approved 또는 rejected")
    if not (isinstance(approver, str) and approver):
        raise ValueError("approver가 비었다")
    entry = store.get(draft_id)
    prior = entry.get("decision")
    if prior is not None:
        if prior.get("decision") == decision and (decision == "rejected"
                                                 or prior.get("body_sha256") == body_sha256):
            return prior
        raise Refused("this draft was already decided")
    if entry.get("result") is not None:
        raise Refused("this draft was already decided")
    t = now()
    record = {"kind": "decision", "v": 1, "draft_id": draft_id, "decision": decision,
              "approver": approver, "decided_at": hdr._iso(t)}
    if decision == "approved":
        if hdr.status_of(entry, t, draft_ttl) != "pending_approval":
            raise Refused("this draft is no longer waiting for approval")
        body = store.body(draft_id)
        draft = _checked(draft_id, entry, body, signer=signer, recipients=recipients,
                         body_max_bytes=body_max_bytes)
        if body_sha256 != draft["body_sha256"]:
            raise Refused("the draft is not the text that was reviewed")
        record.update(to=dict(draft["to"]), media_type=draft["media_type"],
                      body_sha256=draft["body_sha256"], body_size=draft["body_size"],
                      expires_at=hdr._iso(t + ttl))
    stored = store.put_decision(record)
    if stored.get("decision") != decision or stored.get("body_sha256") != record.get("body_sha256"):
        raise Refused("this draft was already decided")     # 그 사이에 다른 결정이 먼저 놓였다
    return stored


# ── 서명하고 올리기 ──────────────────────────────────────────────────────────

class _Ctx:
    """차례를 도는 데 드는 것. 서명 키의 씨앗이 여기에만 있다."""

    def __init__(self, *, hub_dir, outbox_dir, door, seed, signer, key_id, key_epoch, drop_url, token,
                 recipients, body_max_bytes=hdr.DRAFT_BODY_MAX_BYTES,
                 now=time.time, wait=hs.MARK_WAIT_SECONDS, mirror_wait=MIRROR_WAIT_SECONDS,
                 timeout=hd.CLIENT_TIMEOUT_SECONDS, received_only=False, trusted_pubkeys=None,
                 outbox_name=None, sleep=time.sleep, clock=time.monotonic):
        self.hub, self.out = Path(hub_dir), Path(outbox_dir)
        if not (self.hub / "hub.json").is_file():
            raise SendStopped("the ledger is not open here; call open_ledger first")
        self.recipients, self.body_max = dict(recipients), int(body_max_bytes)
        # 묶음 안에서 이 발신함을 부르는 이름. 되살린 원장이면 그 묶음에 적힌 이름과 같아야 한다 —
        # 다르면 체크포인트가 「지난번에 실은 발신함이 빠졌다」로 멈춘다. 기본은 명령줄과 같이 디렉터리의 이름이다.
        self.name = outbox_name or self.out.name
        self.door, self.seed = door, seed
        self.signer, self.key_id, self.key_epoch = signer, key_id, key_epoch
        self.drop_url, self.token = drop_url.rstrip("/"), token
        self.door_url = f"{self.drop_url}/v0/{door}"
        self.now, self.wait, self.mirror_wait, self.timeout = now, wait, mirror_wait, timeout
        self.received_only, self.trusted_pubkeys = received_only, trusted_pubkeys
        self.sleep, self.clock = sleep, clock


def send_approved(store, draft_id: str, **cfg) -> dict:
    """승인된 초안 하나를 문에 보낸다. 돌려주는 것은 결과의 기록(`status`가 `sent`나 `failed`),
    또는 아직 문에 닿지 못했을 때의 `{"status": "approved", "stage": …}`다. 뒤의 것이면 다시 부른다 —
    새 번호도 새 봉투도 생기지 않는다.

    인자: `hub_dir`, `outbox_dir`, `door`(`<channel>/<from-x>`), `seed`(씨앗의 값이다. 경로가
    아니다), `signer`, `key_id`, `key_epoch`, `drop_url`, `token`, `recipients`(받는 연구소 →
    to_epoch. 초안 창구의 것과 같은 표다). `outbox_name`은 묶음 안의 발신함 이름이다(기본은
    디렉터리의 이름). 그 밖은 기다리는 시간과 시험용 시계다."""
    c = _Ctx(**cfg)
    with ho.hub_write_lock(c.hub, name=SEND_LOCK):
        entry = store.get(draft_id)
        if entry.get("result") is not None:
            return entry["result"]
        decision = entry.get("decision")
        if decision is None:
            raise SendStopped("this draft has not been approved")
        if decision.get("decision") != "approved":
            return {"status": "rejected", "draft_id": draft_id}
        # 앞서 서명된 편지부터 문에 보낸다. 그것이 가기 전에는 이 편지에 번호를 주지 않는다.
        progress = _drain(store, c)
        entry = store.get(draft_id)
        if entry.get("result") is not None:
            return entry["result"]
        if not progress["clear"]:
            return _progress(draft_id, progress)
        try:
            _sign(store, entry, c)                     # 발신함에 놓는 것은 뒤의 drain이 한다
        except Refused as e:
            return store.put_result({"kind": "result", "v": 1, "draft_id": draft_id,
                                     "status": "failed", "reason": str(e), "number_used": False,
                                     "failed_at": hdr._iso(c.now())})
        progress = _drain(store, c)
        entry = store.get(draft_id)
        return entry["result"] if entry.get("result") is not None else _progress(draft_id, progress)


def drain(store, **cfg) -> dict:
    """서명됐으나 아직 문에 없는 편지를 문에 보내고 결과를 적는다. 새로 서명하지 않는다."""
    c = _Ctx(**cfg)
    with ho.hub_write_lock(c.hub, name=SEND_LOCK):
        return _drain(store, c)


def resume(store, **cfg) -> list:
    """승인됐고 결과가 없는 초안을 찾아 잇는다(LxM 184 §1). 서비스가 뜰 때와 승인 화면이 열릴 때
    부른다 — 잠들었다 깬 서버에는 돌던 차례의 기억이 없다. 승인된 차례대로 하나씩 보내고, 하나가
    문에 닿지 못하면 거기서 멈춘다(그 뒤의 것에 번호를 주지 않는다). 모델 쪽에서 부르는 길은 없다."""
    c = _Ctx(**cfg)
    # 함의 `approved`가 결정의 기록에서 찾는다. 초안을 훑지 않는다 — 창구의 계정이 새어 초안이 쌓여도
    # 승인해 둔 편지가 묻히지 않는다(LxM 186 §5). 함이 준 것도 다시 본다.
    todo = [e for e in store.approved(c.signer, RESUME_SCAN)
            if e.get("result") is None and (e.get("decision") or {}).get("decision") == "approved"]
    done = []
    for e in todo:
        done.append(send_approved(store, e["draft"]["draft_id"], **cfg))
        if done[-1].get("status") == "approved":
            break
    return done


def open_ledger(*, hub_dir, outbox_dir, door, drop_url, token, trusted_pubkeys, outbox_name=None,
                wait=hs.RESTORE_WAIT_SECONDS, timeout=hd.CLIENT_TIMEOUT_SECONDS, accept_timeout=False,
                received_only=False, sleep=time.sleep, clock=time.monotonic) -> dict:
    """서명하기 전에 원장을 연다. 빈 자리면 드롭의 상태 칸에서 되살리고, 어느 쪽이든 발신함을 문과
    맞댄다. 어긋나면 `SendStopped`다 — 그 자리에서는 서명하지 않는다.

    디스크가 없는 배치는 뜰 때마다 이것을 부른다(LxM 184 §4). `trusted_pubkeys`는 밖에서 믿은 이
    연구소의 공개키들이다 — 세대마다 서명한 키가 다를 수 있고, 묶음 안에 적힌 키는 믿지 않는다."""
    hub, out = Path(hub_dir), Path(outbox_dir)
    name = outbox_name or out.name
    door_url = f"{drop_url.rstrip('/')}/v0/{door}"
    report = {"restored": False}
    if not (hub / "hub.json").is_file():
        try:
            r = hs.restore_from_slot(hub, {name: out}, drop_url=drop_url, token=token,
                                     pubkey_hex=trusted_pubkeys, wait=wait, timeout=timeout,
                                     accept_timeout=accept_timeout, received_only=received_only,
                                     sleep=sleep, clock=clock)
            for n, info in r["outboxes"].items():
                if info["bodies_to_fetch"] and info.get("door"):
                    hs.fetch_bodies(out, f"{drop_url.rstrip('/')}/v0/{info['door']}", token,
                                    info["bodies_to_fetch"], timeout=timeout)
        except hs.StateError as e:
            raise SendStopped(f"could not restore the ledger: {e}") from None
        except OSError as e:
            raise SendStopped(f"the drop did not answer while restoring: {e}") from None
        if r["outboxes"].get(name, {}).get("door") != door:
            # 다른 문의 발신함이다. 되살린 것을 치우고 멈춘다 — 이 자리는 비어 있었다.
            shutil.rmtree(hub, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
            raise SendStopped("the restored outbox belongs to another door")
        report.update(restored=True, generation=r["generation"], signed_by=r["signed_by"],
                      events=r["events"])
    known = (hs.load_record(hub).get("outboxes") or {}).get(name, {}).get("door")
    if known is not None and known != door:
        raise SendStopped("this ledger's outbox belongs to another door")
    c = _Probe(door_url, token, timeout)
    try:
        local, remote = _complete(out), _door(c)
    except _Later as e:
        raise SendStopped(f"could not compare the outbox with the door: {e}") from None
    _compare(local, remote)
    report.update(letters=len(local), not_on_the_door=[n for n in local if n not in remote])
    return report


class _Probe:
    """문의 색인을 읽는 데 드는 것만."""

    def __init__(self, door_url, token, timeout):
        self.door_url, self.token, self.timeout = door_url, token, timeout


def _progress(draft_id: str, progress: dict) -> dict:
    return {"status": "approved", "draft_id": draft_id, "stage": progress["stage"],
            "detail": progress.get("detail")}


def _sign(store, entry: dict, c: _Ctx) -> str:
    """봉투를 짓고 서명해 원장에 넣는다. 같은 초안이면 처음의 사건이 돌아온다 — 새 봉투를 짓지 않는다."""
    draft, decision = entry["draft"], entry["decision"]
    did = draft["draft_id"]
    body = store.body(did)
    with ho.hub_writer(c.hub) as (d, cfg, hub):
        try:
            env = ho.build_message_envelope(
                cfg, signer=c.signer, key_id=c.key_id, epoch=c.key_epoch,
                to_lab=draft["to"]["lab_id"], to_id=draft["to"]["to_id"],
                to_epoch=draft["to"]["to_epoch"], body=body, body_locator=f"draft:{did}",
                media_type=draft["media_type"], created_at=hdr._iso(c.now()))
        except (KeyError, TypeError):
            raise Refused("the draft record is malformed") from None
        prior = hub.idem_prior(c.signer, "message.posted", env["idempotency_key"])
        if prior is None:
            # 아직 서명하지 않았다. 여기서만 초안과 승인을 다시 본다 — 서명한 뒤에는 승인의 끝이 편지를 막지 않는다.
            _checked(did, entry, body, signer=c.signer, recipients=c.recipients,
                     body_max_bytes=c.body_max)
            if (decision.get("to"), decision.get("media_type")) != (draft["to"], draft["media_type"]):
                raise Refused("the approval does not match the draft")
            if not (hashlib.sha256(body).hexdigest() == draft["body_sha256"] == decision.get("body_sha256")
                    and len(body) == draft["body_size"] == decision.get("body_size")):
                raise Refused("the draft is not the text that was approved")
            if hdr.epoch_of(decision["expires_at"]) <= c.now():
                raise Refused("the approval expired before the letter was signed")
        r = ho.sign_and_admit(d, cfg, hub, env, c.seed)
        if not r.get("admitted"):
            raise SendStopped(f"the ledger did not admit the letter: {r.get('problems')}")
        return r["event_id"]


def _place_signed(store, c: _Ctx) -> list:
    """원장에 섰으나 발신함에 없는 이 서명자의 편지를, **원장에 선 차례대로** 발신함에 놓는다. 놓은
    번호들을 돌려준다.

    서명해 원장에 넣고 발신함에 놓기 전에 죽은 자리를 잇는 걸음이자, 방금 서명한 편지를 놓는 걸음이다
    — 길이 하나다. 처음의 봉투와 서명을 그대로 쓴다. 본문은 함에서 받아 봉투의 지문과 맞댄다. 다르거나
    없으면 멈춘다: 새 봉투를 짓지 않고, 그 편지를 건너뛰어 번호를 주지도 않는다(Orin 062 R1)."""
    placed_now = []
    with ho.hub_write_lock(c.hub):
        _clean_incomplete(c.out)
        placed = {he.event_id_of(raw) for raw in _complete(c.out).values()}
        log = c.hub / "events.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        for line in lines:
            rec = json.loads(line)
            if rec.get("transport") != "direct":
                continue
            raw = rec["raw"].encode("utf-8")
            env = json.loads(raw)
            if env.get("event_kind") != "message.posted" or (env.get("signer") or {}).get("id") != c.signer:
                continue
            event_id = he.event_id_of(raw)
            if event_id in placed:
                continue
            did = _draft_of(raw)
            if did is None:
                raise SendStopped(f"the ledger holds a signed letter ({event_id[:16]}…) that is in no outbox "
                                  "and did not come from a draft; place it by hand before sending")
            try:
                body = store.body(did)
            except LookupError:
                raise SendStopped(f"the draft of a signed letter ({event_id[:16]}…) is gone from the "
                                  "store; its body cannot be placed") from None
            payload = env.get("payload") or {}
            if hashlib.sha256(body).hexdigest() != payload.get("body_sha256"):
                raise SendStopped(f"the stored draft no longer matches the signed letter ({event_id[:16]}…)")
            with tempfile.TemporaryDirectory(prefix="organum-send-") as tmp:
                bp = Path(tmp) / ("body" + _SUFFIX.get(payload.get("body_media_type"), ".txt"))
                bp.write_bytes(body)
                placed_now.append(ho.export_quad(c.hub, c.out, event_id=event_id, body_path=bp)["nnn"])
            placed.add(event_id)
    return placed_now


def _complete(out: Path) -> dict:
    """발신함의 완결된 quad. 번호 → 봉투의 바이트. 봉투가 완결의 표지다."""
    quads = {}
    if out.is_dir():
        for f in out.iterdir():
            n = f.name.split("-", 1)[0]
            if f.name == f"{n}-envelope.json" and ho.quad_number(f.name) is not None:
                quads[n] = f.read_bytes()
    return dict(sorted(quads.items(), key=lambda kv: int(kv[0])))


def _clean_incomplete(out: Path) -> None:
    """봉투 없이 남은 조각을 치운다. 쓰다가 죽은 자리다 — 올라간 적이 없고(올리려면 봉투가 든다),
    그대로 두면 그 번호가 비고 저장이 거부된다."""
    if not out.is_dir():
        return
    done = set(_complete(out))
    for f in list(out.iterdir()):
        n = f.name.split("-", 1)[0]
        if ho.quad_number(f.name) is not None and n not in done:
            f.unlink()


def _draft_of(raw: bytes) -> str | None:
    try:
        locator = json.loads(raw.decode("utf-8"))["payload"].get("body_locator")
    except (ValueError, KeyError, AttributeError, UnicodeDecodeError):
        return None
    if isinstance(locator, str) and locator.startswith("draft:") and hdr._DRAFT_ID_RE.match(locator[6:]):
        return locator[6:]
    return None


def _door(c) -> dict:
    """문의 색인. 번호 → 색인의 항목. 드롭이 답하지 못하면 `_Later`다."""
    out, since = {}, "000"
    while True:
        try:
            st, body = hs._http(f"{c.door_url}?index=1&since={since}", c.token, timeout=c.timeout)
        except OSError as e:
            raise _Later(f"the drop did not answer: {e}") from None
        if st == 429 or st >= 500:
            raise _Later(f"the drop answered HTTP {st}")
        if st != 200:
            raise SendStopped(f"could not read the door: HTTP {st} {body.get('error', '')}")
        page = body.get("index")
        if not isinstance(page, list):
            raise SendStopped("the drop has no door index (needs organum 0.8.0 or later)")
        out.update({e["n"]: e for e in page})
        if not body.get("more"):
            return out
        if not page:
            raise SendStopped("the door index does not advance")
        since = page[-1]["n"]


def _drain(store, c: _Ctx) -> dict:
    """`clear`가 참이면 발신함의 모든 편지가 문에 있다. 그때만 다음 편지에 번호를 준다."""
    try:
        return _drain_once(store, c)
    except _Later as e:
        return {"clear": False, "waiting": None, "stage": "door", "detail": str(e)}


def _compare(local: dict, remote: dict) -> None:
    """발신함과 문. 같은 번호에 다른 편지가 있거나 문에 이 발신함이 모르는 편지가 있으면 멈춘다."""
    for n, raw in local.items():
        if n in remote and remote[n].get("envelope_sha256") != hashlib.sha256(raw).hexdigest():
            raise SendStopped(f"letter {n} on the door differs from the outbox")
    strangers = sorted(set(remote) - set(local), key=int)
    if strangers:
        # 이 발신함을 홀로 쥐지 못하고 있다. 번호를 매기는 곳이 둘이면 멈춘다.
        raise SendStopped(f"the door has letters this outbox does not: {', '.join(strangers)}")


def _drain_once(store, c: _Ctx) -> dict:
    _place_signed(store, c)                    # 원장에 선 편지가 먼저다. 그 뒤에야 문과 맞대고 묶음을 뜬다
    local = _complete(c.out)
    remote = _door(c)
    _compare(local, remote)
    pending = [n for n in local if n not in remote]
    detail = None
    if pending:
        try:
            cp = hs.checkpoint(c.hub, {c.name: c.out}, {c.name: c.door}, seed=c.seed,
                               drop_url=c.drop_url, token=c.token, wait=c.wait, timeout=c.timeout,
                               received_only=c.received_only, trusted_pubkeys=c.trusted_pubkeys,
                               sleep=c.sleep, clock=c.clock)
        except hs.StateError as e:
            raise SendStopped(f"checkpoint stopped: {e}") from None
        except OSError as e:
            return {"clear": False, "waiting": pending, "stage": "checkpoint",
                    "detail": f"the drop did not answer: {e}"}
        if not (cp.get("ready_to_push") or (c.received_only and cp.get("received_only"))):
            return {"clear": False, "waiting": pending, "stage": "checkpoint",
                    "detail": "the bundle was received but not confirmed outside yet"}
        try:
            for n in pending:                          # 번호의 차례대로. 문에 빈 번호를 남기지 않는다
                hd.push_quad(c.door_url, c.token, c.out / n, timeout=c.timeout, warmup=False)
        except hd.DropError as e:
            if not (e.status == 429 or e.status >= 500):
                raise SendStopped(f"the drop refused the letter: {e}") from None
            return {"clear": False, "waiting": pending, "stage": "push",
                    "detail": f"the drop did not take the letter: {e}"}
        except OSError as e:
            return {"clear": False, "waiting": pending, "stage": "push",
                    "detail": f"the drop did not answer: {e}"}
        remote = _door(c)
    # 되읽기. 표지를 쓰는 배치에서는 바깥에 옮겨진 것까지 기다린다.
    deadline = c.clock() + c.mirror_wait
    while True:
        late = [n for n in local if n in remote and remote[n].get("mirrored") is False]
        if not late or c.clock() >= deadline:
            break
        c.sleep(hs.MARK_POLL_SECONDS)
        remote = _door(c)
    waiting = []
    for n, raw in local.items():
        item = remote.get(n)
        sha = hashlib.sha256(raw).hexdigest()
        if item is None or item.get("envelope_sha256") != sha:
            waiting.append(n)
            detail = "the letter is not on the door yet"
            continue
        if item.get("mirrored") is False:
            waiting.append(n)
            detail = "the letter is on the door but not stored outside yet"
            continue
        did = _draft_of(raw)
        if did is None:
            continue                                   # 초안에서 온 편지가 아니다(되살린 옛 편지, 키의 기록)
        try:
            if store.get(did).get("result") is not None:
                continue
        except LookupError:
            continue
        store.put_result({"kind": "result", "v": 1, "draft_id": did, "status": "sent",
                          "door": c.door, "n": n, "event_id": he.event_id_of(raw),
                          "envelope_sha256": sha, "sent_at": hdr._iso(c.now())})
    return {"clear": not waiting, "waiting": waiting, "stage": "door" if waiting else "done",
            "detail": detail}
