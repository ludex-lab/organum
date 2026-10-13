"""초안 창구(0.10.0) — 모델이 부르고, 키가 없다. 놓기와 읽기뿐이다.

계약:
1. 이 모듈과 그 바탕은 서명도 원장도 드롭의 쓰기도 들여오지 않는다.
2. 도구는 둘이다. 보내는 도구가 없다.
3. `mail_draft`는 받은 바이트를 그대로 함에 놓는다. 보내지 않는다.
4. 같은 것을 다시 놓으면 같은 초안이 돌아온다. 초안은 바뀌지 않는다.
5. 받는 연구소는 적어 둔 목록 안이어야 한다. 본문과 기다리는 초안의 수에 한도가 있다.
6. `mail_outbox`는 초안마다 지금의 자리를 준다. `sent`만이 「갔다」다. 본문은 돌려주지 않는다.
7. 남의 초안은 없는 초안과 같은 답을 받는다.
8. 두 시대, 머리의 검증, 응답의 한도는 읽기 창구와 같다.
9. 누가 승인하는가를 호스팅이 내건다(0.10.1). 내걸지 않으면 0.10.0의 글 그대로다. 스스로 승인한다고
   내걸면 모델이 듣는 어느 글도 「보내지 않는다」고 말하지 않는다. 달라지는 것은 글뿐이다.
10. 스스로 승인하는 창구의 글은 기다리는 초안이 사람의 승인까지 붙잡힌다고 말하지 않고, 창구가 보지
    못한 서명하는 쪽의 상태를 단정하지 않는다(Orin 064).

실제 키와 실제 드롭은 쓰지 않는다. 연구소의 이름은 꾸민 것이다.
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import hub_draft as hdr        # noqa: E402
from organum import hub_front as hf         # noqa: E402

HQ = "lab:testonly-hq"
PEER = "lab:testonly-peer"
OTHER = "lab:testonly-other"
MODERN = "2026-07-28"
META = {hf.META_VERSION: MODERN, hf.META_CLIENT_CAPABILITIES: {}}
T0 = 1791504000.0                           # 2026-10-09T00:00:00Z


class _Now:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def _front(store=None, **kw):
    store = store if store is not None else hdr.MemoryStore()
    grants = kw.pop("grants", {"jdot": {"sender": HQ, "to": {PEER: 1, OTHER: 2}}})
    kw.setdefault("now", lambda: T0)
    return hdr.DraftFront(store, grants, **kw), store


def _call(front, name, args=None, *, caller="jdot", id_=7):
    st, out = front.handle({"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                            "params": {"name": name, "arguments": args or {}}}, caller)
    assert st == 200 and "result" in out, out
    return out["result"], json.loads(out["result"]["content"][0]["text"])


def _draft(front, body="hello", **kw):
    args = {"to_lab": PEER, "to_id": "Cody", "body": body, **kw}
    return _call(front, "mail_draft", args)


def _sha(b):
    return hashlib.sha256(b).hexdigest()


# ── 자리 ─────────────────────────────────────────────────────────────────────

def test_the_draft_front_brings_in_no_signing_code():
    """초안 창구가 도는 프로세스에는 서명하는 코드가 들어오지 않는다. 뚫려도 서명할 길이 없다."""
    banned = {"hub_ops", "hub_state", "hub_send", "hub_envelope", "hub_drop", "hub_cli", "hub_wire",
              "schnorr_pure", "hub"}
    for name in ("hub_draft.py", "hub_front.py"):
        tree = ast.parse((SRC / "organum" / name).read_text(encoding="utf-8"))
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                seen |= {a.name for a in node.names} | ({node.module} if node.module else set())
            elif isinstance(node, ast.Import):
                seen |= {a.name.split(".")[-1] for a in node.names}
        assert not (seen & banned), (name, seen & banned)


def test_there_are_two_tools_and_neither_sends():
    f, _ = _front()
    st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                        "params": {"_meta": dict(META)}}, "jdot")
    tools = {t["name"]: t for t in out["result"]["tools"]}
    assert st == 200 and sorted(tools) == ["mail_draft", "mail_outbox"]
    assert tools["mail_draft"]["annotations"]["readOnlyHint"] is False     # 쓰는 도구라고 숨기지 않는다
    assert tools["mail_outbox"]["annotations"]["readOnlyHint"] is True
    assert "does NOT send" in tools["mail_draft"]["description"]
    # legacy로도 같은 둘이다
    st, out = f.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "jdot")
    assert [t["name"] for t in out["result"]["tools"]] == ["mail_draft", "mail_outbox"]
    st, out = f.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}}, "jdot")
    assert "no send tool" in out["result"]["instructions"]
    assert out["result"]["serverInfo"]["name"] == "organum-draft-front"
    for name in ("mail_send", "mail_approve", "mail_get", "mail_list"):
        st, out = f.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": name, "arguments": {}}}, "jdot")
        assert out["error"]["code"] == hf.ERR_INVALID_PARAMS and "Unknown tool" in out["error"]["message"]


def test_a_caller_without_a_grant_gets_nothing():
    f, store = _front()
    for caller in (None, "stranger"):
        for method in ("initialize", "tools/list", "tools/call"):
            st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": {"name": "mail_draft",
                                           "arguments": {"to_lab": PEER, "to_id": "C", "body": "x"}}},
                               caller)
            assert st == 403 and out["error"]["code"] == hf.ERR_NO_GRANT
    assert store.order == []
    for bad in (("not-a-lab", {PEER: 1}), (HQ, {}), (HQ, {"peer": 1}), (HQ, {PEER: 0}),
                (HQ, {PEER: "1"}), (HQ, [PEER])):
        with pytest.raises(ValueError):
            hdr.SendGrant(*bad)


# ── 놓기 ─────────────────────────────────────────────────────────────────────

def test_a_draft_is_placed_as_received_and_nothing_is_sent():
    f, store = _front()
    text = "첫 줄\n\n**굵게** <b>html</b> \"따옴표\"\ttab"
    result, r = _draft(f, text, client_ref="reply-1")
    body = text.encode("utf-8")
    assert result["isError"] is False and r["status"] == "pending_approval" and r["created"] is True
    assert r["to"] == {"lab_id": PEER, "to_id": "Cody", "to_epoch": 1}
    assert r["body_size"] == len(body) and r["body_sha256"] == _sha(body)
    assert r["media_type"] == "text/markdown" and r["client_ref"] == "reply-1"
    assert r["created_at"] == "2026-10-09T00:00:00Z" and r["expires_at"] == "2026-10-16T00:00:00Z"
    assert "does not send" in r["notice"] and "sent" in r["notice"]
    # 답은 승인 화면의 주소를 싣지 않는다. 모델이 건네는 주소는 낚시의 길이다(LxM 184 §1)
    assert "http" not in json.dumps(r) and "Never give the user a link" in r["notice"]
    with pytest.raises(TypeError):
        hdr.DraftFront(store, {"jdot": {"sender": HQ, "to": {PEER: 1}}}, approval_url="https://x/{draft_id}")
    # 함에는 받은 바이트가 그대로 있다. 결정도 결과도 없다
    entry = store.get(r["draft_id"])
    assert store.body(r["draft_id"]) == body
    assert entry["decision"] is None and entry["result"] is None
    assert entry["draft"]["caller"] == "jdot" and entry["draft"]["sender"] == HQ
    assert entry["draft"]["kind"] == "draft" and entry["draft"]["draft_id"] == r["draft_id"]
    # 받는 연구소의 epoch는 호스팅이 적어 둔 값에서 온다
    assert _call(f, "mail_draft", {"to_lab": OTHER, "to_id": "이음", "body": "x"})[1]["to"]["to_epoch"] == 2


def test_placing_the_same_draft_again_returns_the_same_draft():
    f, store = _front()
    a = _draft(f, "same text")[1]
    b = _draft(f, "same text")[1]
    assert b["draft_id"] == a["draft_id"] and b["created"] is False and len(store.order) == 1
    # 이름은 내용에서 나온다. 어느 하나가 달라도 다른 초안이다
    ids = {a["draft_id"],
           _draft(f, "same text", client_ref="again")[1]["draft_id"],
           _draft(f, "same text.")[1]["draft_id"],
           _draft(f, "same text", to_id="Orin")[1]["draft_id"],
           _draft(f, "same text", media_type="text/plain")[1]["draft_id"],
           _call(f, "mail_draft", {"to_lab": OTHER, "to_id": "Cody", "body": "same text"})[1]["draft_id"]}
    assert len(ids) == 6 and len(store.order) == 6
    assert a["draft_id"] == hdr.draft_id(sender=HQ, to_lab=PEER, to_id="Cody", media_type="text/markdown",
                                         body_sha256=_sha(b"same text"), client_ref="")
    # 이미 끝난 초안을 다시 놓으면 지금의 자리가 돌아온다. 새로 놓이지 않는다
    store.put_decision({"draft_id": a["draft_id"], "decision": "approved", "decided_at": "x"})
    store.put_result({"draft_id": a["draft_id"], "status": "sent", "door": "hub-ops/from-testonly-hq",
                      "n": "008", "event_id": "e" * 64, "sent_at": "2026-10-09T00:05:00Z"})
    again = _draft(f, "same text")[1]
    assert again["status"] == "sent" and again["created"] is False and again["n"] == "008"
    assert store.body(a["draft_id"]) == b"same text"


def test_what_a_draft_may_be():
    f, store = _front(body_max_bytes=100)
    for args, word in (({"to_lab": "lab:nobody"}, "not allowed"), ({"to_lab": HQ}, "not allowed"),
                       ({"to_lab": 7}, "not allowed"), ({"to_id": ""}, "to_id"), ({"to_id": " x"}, "to_id"),
                       ({"to_id": "a\nb"}, "to_id"), ({"to_id": "x" * 65}, "to_id"), ({"to_id": 3}, "to_id"),
                       ({"media_type": "text/html"}, "media_type"),
                       ({"client_ref": "has space"}, "client_ref"), ({"client_ref": "x" * 65}, "client_ref"),
                       ({"body": ""}, "non-empty"), ({"body": "   \n"}, "non-empty"),
                       ({"body": ["x"]}, "non-empty"), ({"body": "\ud800"}, "Unicode"),
                       ({"body": "x" * 101}, "101 bytes"), ({"body": "한" * 34}, "102 bytes")):
        result, r = _call(f, "mail_draft", {"to_lab": PEER, "to_id": "Cody", "body": "ok", **args})
        assert result["isError"] is True and word in r["error"], (args, r)
    assert store.order == []                                        # 거절은 아무것도 놓지 않는다
    assert _draft(f, "x" * 100)[1]["status"] == "pending_approval"   # 한도까지는 받는다


def test_drafts_waiting_for_approval_are_limited():
    """예약에서도 불린다. 사람 없이 초안이 끝없이 쌓이지 않게 한다."""
    now = _Now()
    f, store = _front(pending_max=3, now=now)
    ids = [_draft(f, f"draft {i}")[1]["draft_id"] for i in range(3)]
    result, r = _draft(f, "one too many")
    assert result["isError"] is True and "waiting for approval" in r["error"] and len(store.order) == 3
    assert _draft(f, "draft 1")[1]["created"] is False               # 이미 놓인 것을 다시 놓는 것은 된다
    # 하나가 결정되면 자리가 난다
    store.put_decision({"draft_id": ids[0], "decision": "rejected", "decided_at": "x"})
    assert _draft(f, "fourth")[1]["created"] is True
    assert _draft(f, "fifth")[0]["isError"] is True
    # 승인 없이 끝난 초안은 세지 않는다
    now.t = T0 + hdr.DRAFT_TTL_SECONDS
    assert _draft(f, "after a week")[1]["created"] is True


def test_the_week_is_measured_by_the_stores_clock_when_it_gives_one():
    """함이 자기 시계로 놓인 때를 주면 그것으로 잰다. 창구가 적어 넣은 때를 믿지 않는다(LxM 184 §2)."""
    clock = _Now()
    store = hdr.MemoryStore(clock=clock)
    f, _ = _front(store, now=clock)
    did = _draft(f, "placed")[1]["draft_id"]
    assert store.get(did)["placed_at"] == "2026-10-09T00:00:00Z"
    # 창구가 끝나는 때를 십 년 뒤로 적어 넣었다고 하자
    store.drafts[did]["draft"]["expires_at"] = "2036-10-09T00:00:00Z"
    entry = store.get(did)
    week = hdr.DRAFT_TTL_SECONDS
    assert hdr.status_of(entry, T0 + week - 1, week) == "pending_approval"
    assert hdr.status_of(entry, T0 + week, week) == "expired"          # 함의 시계에서 이레다
    assert hdr.status_of(entry, T0 + week) == "pending_approval"       # 보는 쪽이 이레를 주지 않으면 기록대로다
    # 기록의 끝이 더 이르면 그것이 먼저다
    store.drafts[did]["draft"]["expires_at"] = "2026-10-10T00:00:00Z"
    assert hdr.status_of(store.get(did), T0 + 86400, week) == "expired"
    # 창구도 그렇게 본다
    store.drafts[did]["draft"]["expires_at"] = "2036-10-09T00:00:00Z"
    clock.t = T0 + week
    assert _call(f, "mail_outbox", {"draft_id": did})[1]["drafts"][0]["status"] == "expired"


# ── 읽기 ─────────────────────────────────────────────────────────────────────

def test_the_outbox_says_where_each_draft_is_and_only_sent_means_sent():
    now = _Now()
    f, store = _front(now=now)
    ids = {k: _draft(f, f"body {k}")[1]["draft_id"]
           for k in ("pending", "approved", "rejected", "sent", "failed")}
    for k in ("approved", "sent", "failed"):
        store.put_decision({"draft_id": ids[k], "decision": "approved", "decided_at": "2026-10-09T00:01:00Z"})
    store.put_decision({"draft_id": ids["rejected"], "decision": "rejected",
                        "decided_at": "2026-10-09T00:02:00Z"})
    store.put_result({"draft_id": ids["sent"], "status": "sent", "door": "hub-ops/from-testonly-hq",
                      "n": "009", "event_id": "ab" * 32, "sent_at": "2026-10-09T00:03:00Z"})
    store.put_result({"draft_id": ids["failed"], "status": "failed", "number_used": False,
                      "reason": "the approval expired before the letter was signed"})
    result, r = _call(f, "mail_outbox")
    by = {d["draft_id"]: d for d in r["drafts"]}
    assert r["status"] == "ok" and r["sender"] == HQ and "notice" in r
    assert [d["draft_id"] for d in r["drafts"]] == list(reversed(list(ids.values())))    # 새것부터
    assert {k: by[v]["status"] for k, v in ids.items()} == {
        "pending": "pending_approval", "approved": "approved", "rejected": "rejected",
        "sent": "sent", "failed": "failed"}
    sent = by[ids["sent"]]
    assert (sent["door"], sent["n"], sent["event_id"], sent["sent_at"]) == \
        ("hub-ops/from-testonly-hq", "009", "ab" * 32, "2026-10-09T00:03:00Z")
    assert "n" not in by[ids["approved"]] and "event_id" not in by[ids["approved"]]   # 승인은 「갔다」가 아니다
    assert by[ids["failed"]]["number_used"] is False and "expired" in by[ids["failed"]]["reason"]
    assert by[ids["rejected"]]["decided_at"] == "2026-10-09T00:02:00Z"
    # 본문은 돌려주지 않는다
    assert "body" not in sent and "body pending" not in result["content"][0]["text"]
    assert sent["body_sha256"] == _sha(b"body sent")
    # 하나만 본다
    one = _call(f, "mail_outbox", {"draft_id": ids["sent"]})[1]["drafts"]
    assert [d["status"] for d in one] == ["sent"]
    assert [d["draft_id"] for d in _call(f, "mail_outbox", {"limit": 2})[1]["drafts"]] == \
        [ids["failed"], ids["sent"]]
    # 승인 없이 이레가 지나면 끝난다
    now.t = T0 + hdr.DRAFT_TTL_SECONDS
    assert _call(f, "mail_outbox", {"draft_id": ids["pending"]})[1]["drafts"][0]["status"] == "expired"
    assert _call(f, "mail_outbox", {"draft_id": ids["approved"]})[1]["drafts"][0]["status"] == "approved"
    for bad in ({"limit": 0}, {"limit": hdr.OUTBOX_LIMIT_MAX + 1}, {"limit": "3"}, {"limit": True},
                {"draft_id": "nope"}, {"draft_id": 7}):
        assert _call(f, "mail_outbox", bad)[0]["isError"] is True, bad


def test_another_senders_draft_looks_like_a_missing_draft():
    store = hdr.MemoryStore()
    f, _ = _front(store, grants={"jdot": {"sender": HQ, "to": {PEER: 1}},
                                 "other": {"sender": OTHER, "to": {PEER: 1}}})
    mine = _draft(f, "mine")[1]["draft_id"]
    theirs = _call(f, "mail_draft", {"to_lab": PEER, "to_id": "C", "body": "theirs"}, caller="other")[1]["draft_id"]
    assert mine != theirs
    missing = _call(f, "mail_outbox", {"draft_id": "0" * 32})
    assert missing[0]["isError"] is True and missing[1] == {"error": "no such draft in this outbox"}
    assert _call(f, "mail_outbox", {"draft_id": theirs})[1] == missing[1]
    assert [d["draft_id"] for d in _call(f, "mail_outbox")[1]["drafts"]] == [mine]
    assert [d["draft_id"] for d in _call(f, "mail_outbox", caller="other")[1]["drafts"]] == [theirs]


# ── 승인의 방식 ──────────────────────────────────────────────────────────────

def _heard(approval=None):
    """모델이 이 창구에서 들을 수 있는 글 전부. 두 시대의 안내, 두 시대의 도구 목록, 두 도구의 답,
    함이 아직일 때의 답, 기다리는 초안이 한도에 닿았을 때의 답."""
    class Waking(hdr.MemoryStore):
        def get(self, draft_id):
            raise hf.NotReady(3)
    kw = {} if approval is None else {"approval": approval}
    f, _ = _front(pending_max=1, **kw)
    out = {}
    st, r = f.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-11-25"}}, "jdot")
    out["instructions legacy"] = r["result"]["instructions"]
    st, r = f.handle({"jsonrpc": "2.0", "id": 2, "method": "server/discover",
                      "params": {"_meta": dict(META)}}, "jdot")
    out["instructions modern"] = r["result"]["instructions"]
    st, r = f.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "jdot")
    out["tools legacy"] = hf._dump(r["result"]["tools"])
    st, r = f.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                      "params": {"_meta": dict(META)}}, "jdot")
    out["tools modern"] = hf._dump(r["result"]["tools"])
    out["mail_draft"] = _draft(f, "first")[0]["content"][0]["text"]
    out["mail_draft again"] = _draft(f, "first")[0]["content"][0]["text"]
    out["mail_outbox"] = _call(f, "mail_outbox")[0]["content"][0]["text"]
    result = _draft(f, "one too many")[0]
    assert result["isError"] is True
    out["waiting"] = result["content"][0]["text"]
    out["not ready"] = _draft(_front(Waking(), **kw)[0])[0]["content"][0]["text"]
    assert len(out) == 9 and all(isinstance(v, str) and v for v in out.values())
    return out


_SAYS_NOT_SENT = ("does not send", "does NOT send", "only places a draft", "must approve",
                  "for the user to approve", "no send tool", "draft letter for approval",
                  "ask the user to approve or reject them first")


def test_without_a_mode_the_front_says_what_0_10_0_said():
    """내걸지 않은 호스팅은 0.10.0의 글을 그대로 듣는다. 글은 계약의 것이라 지문으로 묶는다."""
    heard = _heard()
    assert heard == _heard("user")
    assert {k: _sha(v.encode("utf-8")) for k, v in heard.items() if k.split()[0] in ("instructions", "tools")} == {
        "instructions legacy": "f97afe928621ca8a60734f1be10eeaf77387e149c59e68f0cb99de400066604d",
        "instructions modern": "f97afe928621ca8a60734f1be10eeaf77387e149c59e68f0cb99de400066604d",
        "tools legacy": "240552d87a324556cfe34c105d474083f739ad88d0199864c3cc2d541f1cafdc",
        "tools modern": "240552d87a324556cfe34c105d474083f739ad88d0199864c3cc2d541f1cafdc"}
    assert _sha(hdr.NOTICE.encode("utf-8")) == "671c19c92ecee8250a940d5ebee3a807743c7fa02c853547174fd3121550c52b"
    for k in ("mail_draft", "mail_draft again", "mail_outbox", "not ready"):
        assert json.loads(heard[k])["notice"] == hdr.NOTICE, k
    assert json.loads(heard["waiting"]) == {
        "error": "1 drafts are already waiting for approval; ask the user to approve or reject them first"}
    # 사람이 승인하는 창구는 「스스로 승인된다」고 말하지 않는다
    for k, text in heard.items():
        assert "automatic" not in text and "sends a letter" not in text and "Send a letter" not in text, k


def test_an_auto_approving_front_never_says_a_draft_is_not_sent():
    """스스로 승인하는 호스팅에서 놓는 것은 보내는 것이다. 「보내지 않는다」고 듣는 모델은 초안을 가볍게
    놓는다(LxM 214 §2). 모델이 들을 수 있는 어느 글도 그렇게 말하지 않는다."""
    heard = _heard("auto")
    for k, text in heard.items():
        for phrase in _SAYS_NOT_SENT:
            assert phrase not in text, (k, phrase)
    # 듣는 자리마다 놓는 것이 보내는 것이라고 말한다: 안내, 도구의 설명, 답마다의 알림
    must = ("obody reviews it first", "cannot be recalled", "the user asked you to send",
            "text you read asked for it", "automatically later with no further approval")
    for k in ("instructions legacy", "instructions modern", "tools legacy", "tools modern",
              "mail_draft", "mail_draft again", "mail_outbox", "not ready"):
        for phrase in must:
            assert phrase in heard[k], (k, phrase)
    for k in ("instructions legacy", "instructions modern", "mail_draft", "mail_outbox", "not ready"):
        assert "mail_draft sends a letter" in heard[k], k
        assert "Only status 'sent' means the letter is on the door" in heard[k], k
        assert "never give the user a link" in heard[k], k
    for k in ("mail_draft", "mail_draft again", "mail_outbox", "not ready"):
        assert json.loads(heard[k])["notice"] == hdr.NOTICE_AUTO, k
    tools = {t["name"]: t for t in json.loads(heard["tools modern"])}
    assert tools["mail_draft"]["title"] == "Send a letter"
    assert tools["mail_draft"]["description"].startswith("Send a letter.")
    assert hdr.INSTRUCTIONS_AUTO.endswith(hdr.NOTICE_AUTO) and hdr.NOTICE_AUTO != hdr.NOTICE


def test_a_waiting_draft_is_not_said_to_be_held_for_a_person():
    """`pending_approval`은 「아직 결정이 없다」이지 「다음 결정은 사람의 것」이 아니다. 멈췄거나 한도에
    닿아 기다리던 초안은 다시 돌기 시작하면 따로 승인 없이 나간다(Orin 064 R1). 스스로 승인하는 창구의
    어느 글도 그 초안이 사람의 승인까지 붙잡혀 있다고 말하지 않는다."""
    heard = _heard("auto")
    held = ("until the user", "waits for the user", "wait for the user", "must approve",
            "held for", "stays pending", "will not be sent until", "only after the user")
    for k, text in heard.items():
        for phrase in held:
            assert phrase not in text, (k, phrase)
    # 알림과 도구의 설명이 둘 다 대기가 끝나는 길을 말한다
    for k in ("instructions legacy", "instructions modern", "mail_draft", "mail_outbox", "not ready"):
        assert "means no decision yet, not that a person will decide" in heard[k], k
        assert "a waiting draft can still be sent automatically later with no further approval" in heard[k], k
        assert "The user can also approve or reject it on their own approval page" in heard[k], k
    for k in ("tools legacy", "tools modern"):
        assert ("still pending_approval is not held back: it can be sent automatically later with no "
                "further approval") in heard[k], k
    # 계약의 글은 걸리는 때를 약속하지 않는다. 호스팅마다 다르다
    for k, text in heard.items():
        assert "within minutes" not in text and "usually" not in text, k


def test_a_full_queue_is_reported_as_what_the_front_saw():
    """창구가 본 것은 결정 없는 초안의 수뿐이다. 서명하는 쪽이 멈췄는지, 하루의 한도를 넘었는지는
    읽을 길이 없다(Orin 064 R2). 이 시험에는 서명하는 쪽이 아예 없다."""
    f, store = _front(approval="auto", pending_max=2)
    for i in range(2):
        assert _draft(f, f"draft {i}")[1]["created"] is True
    result, r = _draft(f, "one too many")
    assert result["isError"] is True and sorted(r) == ["error"]
    assert len(store.order) == 2                                     # 새 초안은 놓이지 않았다
    said = r["error"]
    assert said.startswith("2 drafts are already waiting without a decision, so this draft was not placed")
    assert "will not be sent" in said
    assert "can still be sent automatically later with no further approval" in said
    assert "on their own approval page" in said
    for diagnosis in ("paused", "limit", "quota", "stopped", "down", "because"):
        assert diagnosis not in said, diagnosis
    assert "http" not in said and "ask the user to approve or reject them first" not in said
    # 이미 놓인 것을 다시 놓는 것은 된다. 한도는 새 초안에만 건다
    assert _draft(f, "draft 0")[1]["created"] is False


def test_the_mode_changes_the_words_and_nothing_else():
    """도구의 이름, 인자, 주석, 놓이는 것, 답의 칸은 두 방식에서 같다."""
    (u, su), (a, sa) = _front(approval="user"), _front(approval="auto")
    lists = []
    for f in (u, a):
        st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                            "params": {"_meta": dict(META)}}, "jdot")
        lists.append({t["name"]: t for t in out["result"]["tools"]})
    tu, ta = lists
    assert sorted(tu) == sorted(ta) == ["mail_draft", "mail_outbox"]
    assert tu["mail_outbox"] == ta["mail_outbox"]
    assert {k: v for k, v in tu["mail_draft"].items() if k not in ("title", "description")} == \
           {k: v for k, v in ta["mail_draft"].items() if k not in ("title", "description")}
    assert ta["mail_draft"]["annotations"]["readOnlyHint"] is False
    assert tu["mail_draft"]["description"] != ta["mail_draft"]["description"]
    calls = [("mail_draft", {"to_lab": PEER, "to_id": "Cody", "body": "같은 편지", "client_ref": "a"}),
             ("mail_draft", {"to_lab": PEER, "to_id": "Cody", "body": "같은 편지", "client_ref": "a"}),
             ("mail_draft", {"to_lab": "lab:testonly-nowhere", "to_id": "Cody", "body": "x"}),
             ("mail_outbox", {}), ("mail_outbox", {"limit": 0})]
    for name, args in calls:
        (ru, du), (ra, da) = _call(u, name, args), _call(a, name, args)
        assert ru["isError"] == ra["isError"] and sorted(du) == sorted(da), (name, args)
        assert {k: v for k, v in du.items() if k != "notice"} == {k: v for k, v in da.items() if k != "notice"}
        if "notice" in du:
            assert (du["notice"], da["notice"]) == (hdr.NOTICE, hdr.NOTICE_AUTO)
    assert su.order == sa.order and len(su.order) == 1
    assert su.drafts == sa.drafts and su.bodies == sa.bodies
    # 스스로 승인한다고 내걸어도 창구가 승인하지는 않는다. 놓인 것은 기다리고, 결정은 서명하는 쪽이 적는다
    assert sa.drafts[sa.order[0]]["decision"] is None and sa.drafts[sa.order[0]]["result"] is None


def test_a_mode_the_contract_does_not_know_is_refused():
    """틀린 값이 조용히 「보내지 않는다」의 글이 되지 않는다."""
    assert hdr.APPROVALS == ("user", "auto")
    for bad in (None, "", "AUTO", "Auto", "auto ", "manual", "person", True, 1, ["auto"], b"auto"):
        with pytest.raises(ValueError):
            _front(approval=bad)
    for good in hdr.APPROVALS:
        assert _front(approval=good)[0]._approval == good


# ── 크기와 실패 ──────────────────────────────────────────────────────────────

def _wire(front, name, args, *, modern=False):
    params = {"name": name, "arguments": args}
    headers = {}
    if modern:
        params["_meta"] = dict(META)
        headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": name}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode()
    st, _, raw = front.handle_http("POST", headers, body, "jdot")
    return st, raw


def test_requests_and_responses_are_bounded():
    f, store = _front()
    # 한도까지의 본문은 요청에 들어간다. 따옴표뿐이어도 그렇다
    for modern in (False, True):
        for unit in ("x", '"', "한"):
            text = unit * (hdr.DRAFT_BODY_MAX_BYTES // len(unit.encode("utf-8")))
            st, raw = _wire(f, "mail_draft", {"to_lab": PEER, "to_id": "Cody", "body": text,
                                              "client_ref": f"m{int(modern)}"}, modern=modern)
            r = json.loads(json.loads(raw)["result"]["content"][0]["text"])
            assert st == 200 and r["body_size"] <= hdr.DRAFT_BODY_MAX_BYTES and r["status"] == "pending_approval"
            assert store.body(r["draft_id"]) == text.encode("utf-8")
            assert len(raw) < 2000                                    # 답에 본문이 없다
    # 요청이 한도를 넘으면 읽기 전에 413이다
    st, raw = _wire(f, "mail_draft", {"to_lab": PEER, "to_id": "Cody", "body": "x" * hdr.REQUEST_MAX_BYTES})
    assert st == 413
    assert hdr.REQUEST_MAX_BYTES > 2 * hdr.DRAFT_BODY_MAX_BYTES and hdr.REQUEST_MAX_BYTES > hf.REQUEST_MAX_BYTES
    # 응답은 한도를 넘지 않는다. 보낼 함이 넘치면 줄여서 다시 부르라고 한다
    g, store2 = _front(response_max_bytes=hf.RESPONSE_MIN_BYTES, pending_max=60)
    for i in range(60):
        assert _call(g, "mail_draft", {"to_lab": PEER, "to_id": "Cody", "body": "x", "client_ref": f"r{i}"})[1]["created"]
    for modern in (False, True):
        st, raw = _wire(g, "mail_outbox", {"limit": 50}, modern=modern)
        result = json.loads(raw)["result"]
        assert st == 200 and len(raw) <= hf.RESPONSE_MIN_BYTES and result["isError"] is True
        assert "smaller limit" in json.loads(result["content"][0]["text"])["error"]
        st, raw = _wire(g, "mail_outbox", {"limit": 10}, modern=modern)
        assert len(raw) <= hf.RESPONSE_MIN_BYTES and json.loads(raw)["result"]["isError"] is False


def test_store_failures_are_reported_and_nothing_is_invented():
    class Waking(hdr.MemoryStore):
        def get(self, draft_id):
            raise hf.NotReady(12.4)

    class Broken(hdr.MemoryStore):
        def list(self, sender, limit, without=None):
            raise hdr.StoreError("bucket answered 403")
    result, r = _draft(_front(Waking())[0])
    assert result["isError"] is False and r["status"] == "not_ready" and r["retry_after_seconds"] == 12
    assert "draft_id" not in r
    result, r = _draft(_front(Broken())[0])
    assert result["isError"] is True and r == {"error": "draft store error: bucket answered 403"}
    assert _call(_front(Broken())[0], "mail_outbox")[0]["isError"] is True


def test_the_memory_store_keeps_the_first_record():
    """함은 없을 때만 놓는다. 초안도, 결정도, 결과도 첫 것이 남는다."""
    store = hdr.MemoryStore()
    rec = hdr.draft_record(caller="jdot", sender=HQ, to_lab=PEER, to_id="C", to_epoch=1,
                           media_type="text/plain", client_ref="", body=b"one", now=T0, ttl=60)
    entry, created = store.put_draft(rec, b"one")
    assert created is True and entry["draft"] == rec
    entry, created = store.put_draft(dict(rec, caller="someone else"), b"two")
    assert created is False and entry["draft"]["caller"] == "jdot" and store.body(rec["draft_id"]) == b"one"
    did = rec["draft_id"]
    assert store.put_decision({"draft_id": did, "decision": "approved"})["decision"] == "approved"
    assert store.put_decision({"draft_id": did, "decision": "rejected"})["decision"] == "approved"
    assert store.put_result({"draft_id": did, "status": "sent", "n": "001"})["n"] == "001"
    assert store.put_result({"draft_id": did, "status": "failed"})["status"] == "sent"
    assert store.list(HQ, 10, without="decision") == [] and store.list(HQ, 10, without="result") == []
    assert len(store.list(HQ, 10)) == 1
    other = hdr.draft_record(caller="jdot", sender=HQ, to_lab=PEER, to_id="C", to_epoch=1,
                             media_type="text/plain", client_ref="b", body=b"two", now=T0, ttl=60)
    store.put_draft(other, b"two")
    assert [e["draft"]["draft_id"] for e in store.list(HQ, 10, without="decision")] == [other["draft_id"]]
    store.put_decision({"draft_id": other["draft_id"], "decision": "approved", "decided_at": "2026-10-09T00:00:05Z"})
    assert store.list(HQ, 10, without="decision") == []
    assert [e["draft"]["draft_id"] for e in store.list(HQ, 10, without="result")] == [other["draft_id"]]
    # 승인됐고 결과가 없는 것. 결정이 적힌 차례대로 나온다. 끝난 것, 거절한 것, 남의 것은 빠진다
    ids = [other["draft_id"]]
    for i, (decision, at) in enumerate((("approved", "2026-10-09T00:00:09Z"), ("rejected", "2026-10-09T00:00:01Z"),
                                        ("approved", "2026-10-09T00:00:02Z"))):
        r = hdr.draft_record(caller="jdot", sender=HQ, to_lab=PEER, to_id="C", to_epoch=1,
                             media_type="text/plain", client_ref=f"c{i}", body=b"z", now=T0, ttl=60)
        store.put_draft(r, b"z")
        store.put_decision({"draft_id": r["draft_id"], "decision": decision, "decided_at": at})
        ids.append(r["draft_id"])
    theirs = hdr.draft_record(caller="x", sender=OTHER, to_lab=PEER, to_id="C", to_epoch=1,
                              media_type="text/plain", client_ref="", body=b"t", now=T0, ttl=60)
    store.put_draft(theirs, b"t")
    store.put_decision({"draft_id": theirs["draft_id"], "decision": "approved", "decided_at": "2026-10-09T00:00:00Z"})
    assert [e["draft"]["draft_id"] for e in store.approved(HQ, 10)] == [ids[3], ids[0], ids[1]]
    assert [e["draft"]["draft_id"] for e in store.approved(HQ, 2)] == [ids[3], ids[0]]
    store.put_result({"draft_id": ids[3], "status": "sent"})
    assert [e["draft"]["draft_id"] for e in store.approved(HQ, 10)] == [ids[0], ids[1]]
    with pytest.raises(LookupError):
        store.get("0" * 32)
    with pytest.raises(LookupError):
        store.body("0" * 32)


def test_status_follows_the_records():
    draft = {"expires_at": "2026-10-16T00:00:00Z"}
    s = lambda **kw: hdr.status_of({"draft": draft, "decision": None, "result": None, **kw}, T0)   # noqa: E731
    assert s() == "pending_approval"
    assert hdr.status_of({"draft": draft, "decision": None, "result": None}, T0 + 7 * 86400) == "expired"
    assert s(decision={"decision": "approved"}) == "approved"
    assert s(decision={"decision": "rejected"}) == "rejected"
    assert s(decision={"decision": "approved"}, result={"status": "sent"}) == "sent"
    assert s(decision={"decision": "approved"}, result={"status": "failed"}) == "failed"
    assert s(decision={"decision": "approved"}, result={"status": "anything else"}) == "failed"
    assert set(hdr.STATUSES) == {"pending_approval", "approved", "sent", "rejected", "expired", "failed"}
    assert hdr.epoch_of("2026-10-09T00:00:00Z") == T0
