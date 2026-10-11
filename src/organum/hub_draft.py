"""hub draft — 초안 창구(0.10.0). 모델이 부르고, 키가 없다.

한 연구소의 발신을 서버 쪽이 맡는 예외의 앞쪽 절반이다(설계: docs/hub-mail-send-v0-design.md).
대화에서 도구를 부르는 에이전트는 여기서 **초안을 놓고** 보낼 함을 **읽는다.** 보내는 도구는 없다.
플랫폼은 쓰는 도구도 사람에게 묻지 않고 부른다(LxM 183) — 그래서 이 도구들이 할 수 있는 일을
「놓기」와 「읽기」에서 멈춘다. 서명은 사람이 모델 밖의 화면에서 승인한 초안에만, 다른 자리
(`hub_send`)에서 한다.

경계 넷:

1. **이 모듈에는 서명도 원장도 드롭의 쓰기도 없다.** 그것들을 들여오지 않는다. 시험이 고정한다.
2. **인증은 밖의 일이다.** 호스팅이 접근 토큰을 검증해 호출자의 이름을 준다. 이름이 `grants`에
   없으면 아무것도 하지 않는다(403).
3. **놓인 초안은 바뀌지 않는다.** `draft_id`가 내용에서 나오므로 같은 것을 다시 놓으면 같은 초안이
   돌아온다. 고치려면 새 초안을 놓는다.
4. **서명되는 것은 여기서 받은 바이트다.** 모델이 대화에 보인 글과 넘긴 글이 같은지를 받는 쪽은
   알 수 없다. 승인 화면이 받은 바이트를 보여 준다.

초안의 함(`store`)은 일곱을 낸다. 호스팅이 짓는다(버킷). 시험과 연결 시험에는 `MemoryStore`를 쓴다:

    store.put_draft(record, body)   → (entry, created)   # 없을 때만 놓는다. 있으면 있는 것을 돌려준다
    store.get(draft_id)             → entry              # 없으면 LookupError
    store.list(sender, limit, without=None) → [entry, …] # 새것부터. without="decision"이면 결정도 결과도
                                                         # 없는 것만, "result"면 결과가 없는 것만
    store.body(draft_id)            → bytes              # 서명하는 쪽이 쓴다
    store.put_decision(record)      → record             # 서명하는 쪽만. 없을 때만. 첫 결정이 남는다
    store.put_result(record)        → record             # 서명하는 쪽만. 없을 때만
    store.approved(sender, limit)   → [entry, …]         # 서명하는 쪽만. 승인됐고 결과가 없는 것. 결정이
                                                         # 적힌 차례대로(오래된 것부터). **결정의 기록에서**
                                                         # 찾는다 — 초안이 아무리 쌓여도 묻히지 않는다

`entry`는 `{"draft": …, "decision": … 또는 None, "result": … 또는 None}`이다. 함이 자기 시계로 잰
놓인 때를 `"placed_at"`으로 함께 주면 이레를 그 시계로 잰다 — 창구가 적어 넣은 때를 믿지 않는다
(LxM 184 §2). 아직 답할 수 없으면 `hub_front.NotReady`, 그 밖의 실패는 `StoreError`다.

함은 **만들기만으로** 선다. 고치기와 지우기가 없다. 놓인 것은 바뀌지 않고 첫 것이 남는다. 초안
창구의 계정은 초안을 만들고 셋을 읽을 수만 있어야 하고, 결정과 결과를 만드는 권한은 서명하는
쪽에만 둔다 — 그 좁힘은 함의 권한에 있다. 한도는 권한이 지키지 못한다: 창구의 계정이 새면 한도
밖의 초안이 놓일 수 있다. 그래서 서명하는 쪽이 함에서 읽은 것을 다시 잰다(`hub_send`).
"""

from __future__ import annotations

import calendar
import hashlib
import re
import time

from . import __version__
from . import hub_front as hf

DRAFT_BODY_MAX_BYTES = 65_536           # 본문의 한도. 실제 편지는 2~10 KB다. 도구의 인자는 256 KB까지 온전했다(LxM 183)
DRAFT_PENDING_MAX = 20                  # 승인을 기다리는 초안의 수. 예약에서도 불리므로 사람 없이 쌓일 수 있다
DRAFT_TTL_SECONDS = 7 * 86_400          # 승인 없이 이만큼 지나면 끝난다
REQUEST_MAX_BYTES = 300_000             # 본문 64 KiB가 JSON에서 부풀어도 들어간다. 더 부푸는 요청은 413이다
OUTBOX_LIMIT_DEFAULT = 20
OUTBOX_LIMIT_MAX = 50
MEDIA_TYPES = ("text/markdown", "text/plain")
STATUSES = ("pending_approval", "approved", "sent", "rejected", "expired", "failed")

_DRAFT_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_CLIENT_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{0,64}\Z")
_TO_ID_MAX = 64

NOTICE = ("mail_draft only places a draft; it does not send. A letter is signed and sent only after "
          "the user approves that exact text on their own approval page, outside this conversation. "
          "Never give the user a link for approving a draft: the user opens the approval page from "
          "their own bookmark. Only status 'sent' means the letter is on the door. 'approved' means "
          "approved but not delivered yet. Never tell the user a letter was sent unless mail_outbox "
          "shows 'sent'.")
INSTRUCTIONS = ("Outgoing mail drafts. mail_draft places a draft for the user to approve; mail_outbox "
                "shows what happened to each draft. There is no send tool. " + NOTICE)


class StoreError(Exception):
    """초안의 함의 실패. 다시 불러도 나아지지 않는다(권한, 깨진 기록)."""


class SendGrant:
    """호출자 하나가 놓을 수 있는 것: 보내는 연구소 하나와, 받는 연구소들. 받는 연구소마다 그 연구소의
    받는 이 epoch를 적는다 — 봉투의 `to_epoch`가 된다."""

    __slots__ = ("sender", "to")

    def __init__(self, sender: str, to: dict):
        if not (isinstance(sender, str) and hf._LAB_RE.match(sender)):
            raise ValueError("sender는 lab:<이름>")
        if not (isinstance(to, dict) and to and all(
                isinstance(k, str) and hf._LAB_RE.match(k) and type(v) is int and v >= 1
                for k, v in to.items())):
            raise ValueError("to는 {받는 연구소: to_epoch}의 표")
        self.sender, self.to = sender, dict(to)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def epoch_of(stamp: str) -> float:
    """`…Z` 시각 → 초. 기록의 시각은 모두 이 꼴이다."""
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))


def draft_id(*, sender: str, to_lab: str, to_id: str, media_type: str, body_sha256: str,
             client_ref: str) -> str:
    """초안의 이름. 내용에서 나온다 — 같은 보내는 이, 받는 이, 글, `client_ref`면 같은 이름이다.
    비밀값이 들지 않는다. 손잡이이고 권한이 아니다(Orin 061 ③)."""
    return hashlib.sha256(hf._dump({"v": 1, "sender": sender, "to_lab": to_lab, "to_id": to_id,
                                    "media_type": media_type, "body_sha256": body_sha256,
                                    "client_ref": client_ref}).encode("utf-8")).hexdigest()[:32]


def draft_record(*, caller: str, sender: str, to_lab: str, to_id: str, to_epoch: int,
                 media_type: str, client_ref: str, body: bytes, now: float, ttl: float) -> dict:
    sha = hashlib.sha256(body).hexdigest()
    return {"kind": "draft", "v": 1,
            "draft_id": draft_id(sender=sender, to_lab=to_lab, to_id=to_id, media_type=media_type,
                                 body_sha256=sha, client_ref=client_ref),
            "caller": caller, "sender": sender,
            "to": {"lab_id": to_lab, "to_id": to_id, "to_epoch": to_epoch},
            "media_type": media_type, "client_ref": client_ref,
            "body_sha256": sha, "body_size": len(body),
            "created_at": _iso(now), "expires_at": _iso(now + ttl)}


def status_of(entry: dict, now: float, max_age: float | None = None) -> str:
    """초안 하나가 지금 어디에 있는가. `sent`만이 「갔다」다.

    `max_age`: 보는 쪽이 정한 이레. 함이 놓인 때(`placed_at`)를 주면 그 시계에서 이만큼 지난 초안은
    끝난 것이다 — 기록에 적힌 끝나는 때가 더 늦어도 그렇다."""
    result, decision = entry.get("result"), entry.get("decision")
    if result is not None:
        return "sent" if result.get("status") == "sent" else "failed"
    if decision is not None:
        return "approved" if decision.get("decision") == "approved" else "rejected"
    end = epoch_of(entry["draft"]["expires_at"])
    if max_age is not None and entry.get("placed_at") is not None:
        end = min(end, epoch_of(entry["placed_at"]) + max_age)
    return "expired" if end <= now else "pending_approval"


# 초안의 칸이 지켜야 하는 것. 창구가 받을 때 보고, 서명하는 쪽이 함에서 읽은 것을 다시 본다.

def check_to_id(to_id) -> None:
    if not (isinstance(to_id, str) and 1 <= len(to_id) <= _TO_ID_MAX and to_id.isprintable()
            and to_id == to_id.strip()):
        raise ValueError(f"to_id must be a plain name of 1 to {_TO_ID_MAX} characters")


def check_media_type(media_type) -> None:
    if media_type not in MEDIA_TYPES:
        raise ValueError("media_type must be one of: " + ", ".join(MEDIA_TYPES))


def check_client_ref(client_ref) -> None:
    if not (isinstance(client_ref, str) and _CLIENT_REF_RE.match(client_ref)):
        raise ValueError("client_ref may use letters, digits and . _ : - up to 64 characters")


def check_body(body: bytes, limit: int) -> None:
    if len(body) > limit:
        raise ValueError(f"body is {len(body)} bytes; the limit is {limit}")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("body is not UTF-8 text") from None
    if not text.strip():
        raise ValueError("body must be non-empty text")


class MemoryStore:
    """메모리의 함. 시험과, 실제 우편에 닿지 않는 연결 시험에 쓴다."""

    def __init__(self, clock=None):
        self.drafts: dict = {}          # draft_id → {"draft", "decision", "result"[, "placed_at"]}
        self.bodies: dict = {}
        self.order: list = []
        self.clock = clock              # 주면 함의 시계로 놓인 때를 적는다(버킷이 하는 일의 흉내)

    def put_draft(self, record: dict, body: bytes):
        did = record["draft_id"]
        if did in self.drafts:
            return self.get(did), False
        self.drafts[did] = {"draft": dict(record), "decision": None, "result": None}
        if self.clock is not None:
            self.drafts[did]["placed_at"] = _iso(self.clock())
        self.bodies[did] = bytes(body)
        self.order.append(did)
        return self.get(did), True

    def get(self, draft_id: str) -> dict:
        try:
            e = self.drafts[draft_id]
        except KeyError:
            raise LookupError(draft_id) from None
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in e.items()}

    def list(self, sender: str, limit: int, without: str | None = None) -> list:
        out = []
        for did in reversed(self.order):
            e = self.drafts[did]
            if e["draft"]["sender"] != sender:
                continue
            if without == "decision" and (e["decision"] is not None or e["result"] is not None):
                continue
            if without == "result" and e["result"] is not None:
                continue
            out.append(self.get(did))
            if len(out) >= limit:
                break
        return out

    def body(self, draft_id: str) -> bytes:
        try:
            return self.bodies[draft_id]
        except KeyError:
            raise LookupError(draft_id) from None

    def approved(self, sender: str, limit: int) -> list:
        found = [(e["decision"].get("decided_at") or "", i, did)
                 for i, did in enumerate(self.order)
                 for e in [self.drafts[did]]
                 if e["draft"]["sender"] == sender and e["result"] is None
                 and (e["decision"] or {}).get("decision") == "approved"]
        return [self.get(did) for _, _, did in sorted(found)[:limit]]

    def put_decision(self, record: dict) -> dict:
        e = self.drafts[record["draft_id"]]
        if e["decision"] is None:
            e["decision"] = dict(record)
        return dict(e["decision"])

    def put_result(self, record: dict) -> dict:
        e = self.drafts[record["draft_id"]]
        if e["result"] is None:
            e["result"] = dict(record)
        return dict(e["result"])


def _tools() -> list:
    return [
        {"name": "mail_draft",
         "title": "Place a draft letter for approval",
         "description": ("Place a draft of an outgoing letter. This does NOT send it. The user must "
                         "approve the exact text on the approval page before it is signed and sent. "
                         "Pass the body exactly as it should be sent. Placing the same draft again "
                         "returns the same draft; to send the same text to the same recipient a "
                         "second time, pass a different client_ref. A draft cannot be edited: place "
                         "a new one. Use mail_outbox to see whether a draft was approved and sent."),
         "inputSchema": {"type": "object",
                         "properties": {"to_lab": {"type": "string",
                                                   "description": "Recipient lab, e.g. lab:example"},
                                        "to_id": {"type": "string",
                                                  "description": "Recipient name inside that lab"},
                                        "body": {"type": "string",
                                                 "description": "The letter text, UTF-8, exactly as it should be sent"},
                                        "media_type": {"type": "string", "enum": list(MEDIA_TYPES)},
                                        "client_ref": {"type": "string",
                                                       "description": "Optional label chosen by the caller"}},
                         "required": ["to_lab", "to_id", "body"], "additionalProperties": False},
         "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                         "openWorldHint": False}},
        {"name": "mail_outbox",
         "title": "See what happened to your drafts",
         "description": ("List your drafts with their status: pending_approval, approved, sent, "
                         "rejected, expired or failed. Only 'sent' means the letter was delivered; it "
                         "then carries the door, the number and the event_id. Draft bodies are not "
                         "returned. Pass draft_id to look at one draft."),
         "inputSchema": {"type": "object",
                         "properties": {"draft_id": {"type": "string"},
                                        "limit": {"type": "integer", "minimum": 1,
                                                  "maximum": OUTBOX_LIMIT_MAX}},
                         "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                         "openWorldHint": False}},
    ]


_TOO_LARGE = {"mail_draft": ("the answer does not fit in one response; the draft may have been placed "
                             "— look it up with mail_outbox"),
              "mail_outbox": "the list does not fit in one response; call mail_outbox with a smaller limit"}


class DraftFront(hf._Front):
    """초안 창구. MCP 요청 하나 → 응답 하나. 상태가 없다. 두 시대와 응답의 한도는 읽기 창구와 같다.

    `grants`: 호출자의 이름 → `SendGrant`(또는 `{"sender": …, "to": {받는 연구소: to_epoch}}`).

    답에 승인 화면의 주소를 싣지 않는다. 그 주소를 사용자에게 건네는 것은 모델이고, 받은 편지의
    글이 모델을 움직여 닮은 주소를 건네게 할 수 있다(LxM 184 §1). 사용자는 저장해 둔 주소로
    들어가고, 화면이 기다리는 초안을 모두 보인다. 답이 싣는 것은 `draft_id`다."""

    _instructions = INSTRUCTIONS

    def __init__(self, store, grants: dict, *, name: str = "organum-draft-front",
                 version: str = __version__,
                 body_max_bytes: int = DRAFT_BODY_MAX_BYTES, pending_max: int = DRAFT_PENDING_MAX,
                 ttl_seconds: float = DRAFT_TTL_SECONDS,
                 budget_seconds: float = hf.BUDGET_SECONDS,
                 response_max_bytes: int = hf.RESPONSE_MAX_BYTES,
                 request_max_bytes: int = REQUEST_MAX_BYTES,
                 allowed_origins=None, modern: bool = True, clock=time.monotonic, now=time.time):
        self._init_front({str(k): (v if isinstance(v, SendGrant) else SendGrant(v["sender"], v["to"]))
                          for k, v in grants.items()},
                         name=name, version=version, budget_seconds=budget_seconds,
                         response_max_bytes=response_max_bytes, allowed_origins=allowed_origins,
                         modern=modern, clock=clock, now=now, request_max_bytes=request_max_bytes)
        self._store = store
        self._body_max = int(body_max_bytes)
        self._pending_max = max(1, int(pending_max))
        self._ttl = float(ttl_seconds)

    def _tool_defs(self) -> list:
        return _tools()

    def _call(self, params: dict, who: tuple, wrap):
        caller, grant = who
        name, args = params.get("name"), params.get("arguments", {})
        if name not in ("mail_draft", "mail_outbox"):
            return hf.ERR_INVALID_PARAMS, f"Unknown tool: {name}"
        if not isinstance(args, dict):
            return hf.ERR_INVALID_PARAMS, "arguments must be an object"
        try:
            out = (self._mail_draft if name == "mail_draft" else self._mail_outbox)(args, caller, grant)
            result = self._shaped(out)
            if not self._fits(result, wrap):
                result = self._shaped({"error": _TOO_LARGE[name]}, True)
        except hf._ToolError as e:
            result = self._shaped({"error": str(e)}, True)
        except hf.NotReady as e:
            result = self._shaped({"status": "not_ready", "notice": NOTICE,
                                   "retry_after_seconds": max(1, int(round(e.retry_after)))})
        except StoreError as e:
            result = self._shaped({"error": f"draft store error: {e}"}, True)
        return result

    # ── 놓기 ──
    def _mail_draft(self, args: dict, caller: str, grant: SendGrant) -> dict:
        to_lab, to_id = args.get("to_lab"), args.get("to_id")
        if not isinstance(to_lab, str) or to_lab not in grant.to:
            raise hf._ToolError("that recipient lab is not allowed for this sender")
        media_type, client_ref = args.get("media_type", MEDIA_TYPES[0]), args.get("client_ref", "")
        text = args.get("body")
        if not isinstance(text, str):
            raise hf._ToolError("body must be non-empty text")
        try:
            body = text.encode("utf-8")
        except UnicodeEncodeError:
            raise hf._ToolError("body is not valid Unicode text") from None
        try:
            check_to_id(to_id)
            check_media_type(media_type)
            check_client_ref(client_ref)
            check_body(body, self._body_max)
        except ValueError as e:
            raise hf._ToolError(str(e)) from None
        now = self._now()
        record = draft_record(caller=caller, sender=grant.sender, to_lab=to_lab, to_id=to_id,
                              to_epoch=grant.to[to_lab], media_type=media_type,
                              client_ref=client_ref, body=body, now=now, ttl=self._ttl)
        try:
            entry, created = self._store.get(record["draft_id"]), False
        except LookupError:
            # 새 초안이다. 기다리는 것이 한도에 닿았으면 받지 않는다 — 사람 없이 쌓이지 않게.
            waiting = [e for e in self._store.list(grant.sender, self._pending_max, without="decision")
                       if status_of(e, now, self._ttl) == "pending_approval"]
            if len(waiting) >= self._pending_max:
                raise hf._ToolError(f"{len(waiting)} drafts are already waiting for approval; ask the "
                                    "user to approve or reject them first") from None
            entry, created = self._store.put_draft(record, body)
        out = self._describe(entry, now)
        out.update(created=created, notice=NOTICE)
        return out

    # ── 읽기 ──
    def _mail_outbox(self, args: dict, caller: str, grant: SendGrant) -> dict:
        limit = args.get("limit", OUTBOX_LIMIT_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= OUTBOX_LIMIT_MAX:
            raise hf._ToolError(f"limit must be an integer from 1 to {OUTBOX_LIMIT_MAX}")
        now = self._now()
        did = args.get("draft_id")
        if did is not None:
            if not (isinstance(did, str) and _DRAFT_ID_RE.match(did)):
                raise hf._ToolError("draft_id is not a draft id")
            try:
                entry = self._store.get(did)
            except LookupError:
                entry = None
            # 남의 초안은 없는 초안과 같은 답을 받는다
            if entry is None or entry["draft"]["sender"] != grant.sender:
                raise hf._ToolError("no such draft in this outbox")
            entries = [entry]
        else:
            entries = self._store.list(grant.sender, limit)
        return {"status": "ok", "sender": grant.sender,
                "drafts": [self._describe(e, now) for e in entries], "notice": NOTICE}

    def _describe(self, entry: dict, now: float) -> dict:
        d, decision, result = entry["draft"], entry.get("decision"), entry.get("result")
        status = status_of(entry, now, self._ttl)
        out = {"draft_id": d["draft_id"], "status": status, "to": dict(d["to"]),
               "media_type": d["media_type"], "client_ref": d["client_ref"],
               "body_size": d["body_size"], "body_sha256": d["body_sha256"],
               "created_at": d["created_at"], "expires_at": d["expires_at"]}
        if decision is not None:
            out["decided_at"] = decision.get("decided_at")
        if status == "sent":
            out.update(door=result.get("door"), n=result.get("n"), event_id=result.get("event_id"),
                       sent_at=result.get("sent_at"))
        elif status == "failed":
            out.update(reason=result.get("reason"), number_used=result.get("number_used"))
        return out
