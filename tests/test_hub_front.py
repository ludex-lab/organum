"""드롭 앞의 MCP 창구(0.9.0) — 읽기 전용. 그리고 그것을 받치는 드롭 서버의 작은 변경 셋.

계약:
1. 창구는 상태가 없다. 요청 하나에 응답 하나이고 세션 id를 내지 않는다. GET은 405다.
2. 두 시대를 다 말한다. `2026-07-28`은 요청마다 `_meta`의 판으로, 그 앞은 `initialize`로.
3. 호출자의 이름이 grants에 없으면 아무것도 내주지 않는다.
4. 받는 이가 그 호출자의 연구소인 편지만 내준다. 남의 앞 편지는 목록에도, 한 통 받기에도 없다.
5. 내준 바이트는 읽는 길이 준 바이트와 같다. 지문을 함께 준다.
6. 커서는 이어 받기다. 범위를 넓히지 못한다.
7. 읽는 길이 아직이면 매달리지 않는다. 「아직이다, 다시 불러라」로 답하고 커서는 본 데까지 간다.
9. 본문은 받는 이를 확인한 뒤에만 읽는다. 남의 앞 본문은 창구의 과정에도 들어오지 않는다.
10. 감사 고리를 주면 가져온 봉투, 연 봉투, 내준 편지를 적는다. 적지 못하면 내주지 않는다.
11. `handle_http`가 돌려주는 바이트는 어떤 요청에도 `response_max_bytes`를 넘지 않는다.
8. 드롭: 쓰기 범위가 하나도 없는 범위 줄은 상태 칸에 쓰지 못한다. 편지 받기의 개수를 줄일 수 있다.

실제 키와 실제 드롭은 쓰지 않는다. 서명 칸은 모양만 맞춘 값이다 — 창구는 서명을 확인하지 않는다.
"""

import base64
import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import hub_cli                 # noqa: E402
from organum import hub_drop as hd          # noqa: E402
from organum import hub_front as hf         # noqa: E402

HQ = "lab:testonly-hq"
OTHER = "lab:testonly-other"
DOOR_A = "hub-ops/from-alpha"
DOOR_B = "hub-ops/from-beta"
SIG = "ab" * 64
MODERN = "2026-07-28"
META = {hf.META_VERSION: MODERN, hf.META_CLIENT_CAPABILITIES: {}}


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _letter(to, body, *, frm="lab:testonly-alpha", created="2026-10-09T00:00:00Z", name="body.md",
            digest=None):
    env = json.dumps({"envelope_schema": "testonly", "event_kind": "message.posted",
                      "signer": {"id": frm, "key_id": "k1", "key_epoch": 1}, "created_at": created,
                      "payload": {"target": {"lab_id": to, "to_id": "T", "to_epoch": 1},
                                  "body_sha256": digest or (_sha(body) if body is not None else None),
                                  "body_media_type": "text/markdown"}},
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"envelope": env, "sig": SIG, "body": body, "body_name": name if body is not None else None}


def _mailbox():
    return {DOOR_A: {"001": _letter(HQ, b"first for hq"),
                     "002": _letter(OTHER, b"SECRET for the other lab"),
                     "003": _letter(HQ, "셋째 편지, 한글".encode("utf-8"))},
            DOOR_B: {"001": _letter(OTHER, b"SECRET two", frm="lab:testonly-beta"),
                     "002": _letter(HQ, b"from beta", frm="lab:testonly-beta")}}


def _front(letters=None, **kw):
    reader = kw.pop("reader", None) or hf.MemoryReader(letters if letters is not None else _mailbox())
    grants = kw.pop("grants", {"jj": {"recipient": HQ, "doors": [DOOR_A, DOOR_B]}})
    kw.setdefault("now", lambda: 1791504000.0)           # 2026-10-09T00:00:00Z
    return hf.MailFront(reader, grants, **kw)


def _call(front, name, args=None, *, caller="jj", id_=7):
    st, out = front.handle({"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                            "params": {"name": name, "arguments": args or {}}}, caller)
    assert st == 200 and "result" in out, out
    return out["result"], json.loads(out["result"]["content"][0]["text"])


def _wire(front, name, args=None, *, modern=False, id_=7):
    """`handle_http`로 부른다 — 실제로 나가는 바이트를 잰다. (보낸 바이트, 도구의 결과, 그 안의 답)"""
    params = {"name": name, "arguments": args or {}}
    headers = {}
    if modern:
        params["_meta"] = dict(META)
        headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": name}
    body = json.dumps({"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": params}).encode()
    st, _, raw = front.handle_http("POST", headers, body, "jj")
    result = json.loads(raw)["result"]
    assert st == 200 and ("resultType" in result) is modern
    return raw, result, json.loads(result["content"][0]["text"])


def _fat(to, body, **fields):
    """봉투의 칸을 마음대로 채운 편지. 드롭은 봉투를 64 KiB까지 받고 안을 보지 않는다."""
    env = {"envelope_schema": "testonly", "event_kind": "message.posted",
           "signer": {"id": fields.pop("frm", "lab:testonly-alpha"), "key_id": "k1", "key_epoch": 1},
           "created_at": fields.pop("created", "2026-10-09T00:00:00Z"),
           "payload": {"target": {"lab_id": to, "to_id": "T", "to_epoch": 1},
                       "body_sha256": _sha(body), "body_media_type": fields.pop("media", "text/markdown")},
           **fields}
    return {"envelope": json.dumps(env, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            "sig": SIG, "body": body, "body_name": "body.md"}


# ── 두 시대 ──────────────────────────────────────────────────────────────────

def test_legacy_opens_with_initialize_and_keeps_no_session():
    f = _front()
    st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "openai-mcp", "version": "1"}}}, "jj")
    r = out["result"]
    assert st == 200 and r["protocolVersion"] == "2025-11-25" and r["capabilities"] == {"tools": {}}
    assert r["serverInfo"]["name"] == "organum-mail-front" and "resultType" not in r
    # 모르는 판을 청하면 우리가 말하는 가장 새 legacy 판으로 답한다
    st, out = f.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                        "params": {"protocolVersion": "1999-01-01"}}, "jj")
    assert out["result"]["protocolVersion"] == hf.LEGACY_VERSIONS[0]
    # initialize를 거치지 않은 요청도 그대로 받는다 — 요청 사이에 기억하는 것이 없다
    st, out = f.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "jj",
                       {"mcp-protocol-version": "2025-11-25"})
    assert st == 200 and [t["name"] for t in out["result"]["tools"]] == ["mail_get", "mail_list"]
    assert all(t["annotations"]["readOnlyHint"] is True for t in out["result"]["tools"])
    assert f.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"}, "jj")[1]["result"] == {}
    # 알림에는 답하지 않는다
    assert f.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, "jj") == (202, None)
    # legacy에서 모르는 메서드는 JSON-RPC 오류다(HTTP는 200)
    st, out = f.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"}, "jj")
    assert st == 200 and out["error"]["code"] == hf.ERR_METHOD_NOT_FOUND
    st, out = f.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, "jj",
                       {"mcp-protocol-version": "1999-01-01"})
    assert st == 400 and out["error"]["data"]["supported"] == list(hf.SUPPORTED_VERSIONS)


def test_modern_carries_the_version_on_every_request():
    f = _front()
    st, out = f.handle({"jsonrpc": "2.0", "id": "d1", "method": "server/discover",
                        "params": {"_meta": dict(META)}}, "jj")
    r = out["result"]
    assert st == 200 and r["resultType"] == "complete"
    assert r["supportedVersions"] == list(hf.SUPPORTED_VERSIONS) and r["capabilities"] == {"tools": {}}
    assert r["_meta"][hf.META_SERVER_INFO]["name"] == "organum-mail-front"
    st, out = f.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                        "params": {"_meta": dict(META)}}, "jj")
    r = out["result"]
    assert r["resultType"] == "complete" and r["ttlMs"] > 0 and r["cacheScope"] == "private"
    assert [t["name"] for t in r["tools"]] == ["mail_get", "mail_list"]      # 차례가 정해져 있다
    st, out = f.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "mail_list", "arguments": {}, "_meta": dict(META)}}, "jj")
    assert st == 200 and out["result"]["resultType"] == "complete" and out["result"]["isError"] is False
    # 모르는 메서드는 404에 -32601이다 — 옛 서버의 404와 본문으로 갈린다
    st, out = f.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list",
                        "params": {"_meta": dict(META)}}, "jj")
    assert st == 404 and out["error"]["code"] == hf.ERR_METHOD_NOT_FOUND
    # 판이 다르면 우리가 말하는 판을 알려 준다. 클라이언트는 그것으로 다시 온다
    st, out = f.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list",
                        "params": {"_meta": {**META, hf.META_VERSION: "2030-01-01"}}}, "jj")
    assert st == 400 and out["error"]["code"] == hf.ERR_UNSUPPORTED_VERSION
    assert out["error"]["data"] == {"supported": list(hf.SUPPORTED_VERSIONS), "requested": "2030-01-01"}
    # 클라이언트 능력 칸은 필수다
    st, out = f.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/list",
                        "params": {"_meta": {hf.META_VERSION: MODERN}}}, "jj")
    assert st == 400 and out["error"]["code"] == hf.ERR_INVALID_PARAMS


def test_modern_headers_must_say_what_the_body_says():
    f = _front()
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "mail_list", "arguments": {}, "_meta": dict(META)}}
    good = {"mcp-protocol-version": MODERN, "mcp-method": "tools/call", "mcp-name": "mail_list"}
    assert f.handle(msg, "jj", good)[0] == 200
    b64 = "=?base64?" + base64.b64encode(b"mail_list").decode() + "?="
    assert f.handle(msg, "jj", {**good, "mcp-name": b64})[0] == 200
    for bad in ({**good, "mcp-protocol-version": "2025-11-25"}, {**good, "mcp-method": "tools/list"},
                {**good, "mcp-name": "mail_get"}, {k: v for k, v in good.items() if k != "mcp-name"},
                {k: v for k, v in good.items() if k != "mcp-method"},
                {**good, "mcp-name": "=?base64?!!!?="}):
        st, out = f.handle(msg, "jj", bad)
        assert st == 400 and out["error"]["code"] == hf.ERR_HEADER_MISMATCH, bad
    # 머리는 modern인데 본문에 판이 없다
    st, out = f.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "jj",
                       {"mcp-protocol-version": MODERN, "mcp-method": "tools/list"})
    assert st == 400 and out["error"]["code"] == hf.ERR_INVALID_PARAMS
    # server/discover는 머리만으로도 답한다 — 처음 묻는 요청이다
    st, out = f.handle({"jsonrpc": "2.0", "id": 3, "method": "server/discover"}, "jj",
                       {"mcp-protocol-version": MODERN, "mcp-method": "server/discover"})
    assert st == 200 and out["result"]["supportedVersions"][0] == MODERN


def test_modern_can_be_switched_off_and_the_client_falls_back():
    """선행 시험에서 본 차례 그대로: `server/discover`(2026-07-28) → 모르는 메서드 → `initialize`."""
    f = _front(modern=False)
    st, out = f.handle({"jsonrpc": "2.0", "id": "d", "method": "server/discover",
                        "params": {"_meta": dict(META)}}, "jj",
                       {"mcp-protocol-version": MODERN, "mcp-method": "server/discover"})
    assert st == 200 and out["error"]["code"] == hf.ERR_METHOD_NOT_FOUND
    st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}}, "jj")
    assert st == 200 and out["result"]["protocolVersion"] == "2025-11-25"
    # modern의 꼴로 온 도구 호출도 legacy로 답한다 — resultType이 없다
    st, out = f.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "mail_list", "arguments": {}, "_meta": dict(META)}}, "jj",
                       {"mcp-protocol-version": "2025-11-25"})
    assert st == 200 and "resultType" not in out["result"] and out["result"]["isError"] is False
    st, out = f.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "jj",
                       {"mcp-protocol-version": MODERN})
    assert st == 400 and out["error"]["data"]["supported"] == list(hf.LEGACY_VERSIONS)
    # 켜 둔 창구에서는 같은 요청에 modern으로 답한다
    st, out = _front().handle({"jsonrpc": "2.0", "id": "d", "method": "server/discover",
                               "params": {"_meta": dict(META)}}, "jj")
    assert st == 200 and out["result"]["resultType"] == "complete"


def test_malformed_messages():
    f = _front()
    assert f.handle({"id": 1, "method": "ping"}, "jj")[0] == 400
    assert f.handle({"jsonrpc": "2.0", "id": 1}, "jj")[0] == 400
    for bad_id in (None, True, 1.5, [1]):
        assert f.handle({"jsonrpc": "2.0", "id": bad_id, "method": "ping"}, "jj")[0] == 400
    st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}, "jj")
    assert st == 400 and out["error"]["code"] == hf.ERR_INVALID_PARAMS
    st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "mail_send", "arguments": {}}}, "jj")
    assert out["error"]["code"] == hf.ERR_INVALID_PARAMS and "Unknown tool" in out["error"]["message"]
    st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "mail_list", "arguments": [1]}}, "jj")
    assert out["error"]["code"] == hf.ERR_INVALID_PARAMS


# ── HTTP 한 겹 ───────────────────────────────────────────────────────────────

def test_http_is_one_request_one_json_response():
    f = _front(allowed_origins=["https://chatgpt.com"])
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25"}}).encode()
    st, headers, raw = f.handle_http("POST", {"Content-Type": "application/json"}, body, "jj")
    assert st == 200 and headers == {"Content-Type": "application/json"}
    assert json.loads(raw)["result"]["protocolVersion"] == "2025-11-25"
    assert not any(k.lower() == "mcp-session-id" for k in headers)        # 세션을 만들지 않는다
    # 길게 여는 GET과 세션을 끊는 DELETE는 405다
    for method in ("GET", "DELETE", "PUT"):
        st, headers, raw = f.handle_http(method, {}, b"", "jj")
        assert st == 405 and headers["Allow"] == "POST"
    # 알림은 202이고 본문이 없다
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    assert f.handle_http("POST", {}, note, "jj") == (202, {}, b"")
    # JSON이 아니거나 한 요청에 여럿이면 400이다
    st, _, raw = f.handle_http("POST", {}, b"{not json", "jj")
    assert st == 400 and json.loads(raw)["error"]["code"] == hf.ERR_PARSE
    st, _, raw = f.handle_http("POST", {}, json.dumps([{"jsonrpc": "2.0"}]).encode(), "jj")
    assert st == 400 and json.loads(raw)["error"]["code"] == hf.ERR_INVALID_REQUEST
    assert f.handle_http("POST", {}, b"x" * (hf.REQUEST_MAX_BYTES + 1), "jj")[0] == 413
    # Origin이 있으면 목록 안이어야 한다. 없으면 보지 않는다
    assert f.handle_http("POST", {"Origin": "https://evil.example"}, body, "jj")[0] == 403
    assert f.handle_http("POST", {"ORIGIN": "https://chatgpt.com"}, body, "jj")[0] == 200
    # 머리 이름은 대소문자를 가리지 않는다
    call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "mail_list", "arguments": {}, "_meta": dict(META)}}).encode()
    st, _, raw = f.handle_http("POST", {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call",
                                        "MCP-NAME": "mail_list"}, call, "jj")
    assert st == 200 and json.loads(raw)["result"]["resultType"] == "complete"


# ── 누가, 무엇을 ─────────────────────────────────────────────────────────────

def test_a_caller_without_a_grant_gets_nothing():
    f = _front()
    for caller in (None, "stranger", 7):
        for method in ("initialize", "tools/list", "server/discover", "tools/call"):
            st, out = f.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": {"_meta": dict(META)}}, caller)
            assert st == 403 and out["error"]["code"] == hf.ERR_NO_GRANT, (caller, method)
    with pytest.raises(ValueError):
        hf.Grant("not-a-lab", [DOOR_A])
    with pytest.raises(ValueError):
        hf.Grant(HQ, ["hub-ops/*"])                      # 문은 적은 것만. 패턴이 아니다
    with pytest.raises(ValueError):
        hf.Grant(HQ, [])


def test_only_letters_addressed_to_the_callers_lab(tmp_path):
    f = _front()
    result, r = _call(f, "mail_list")
    assert result["isError"] is False and r["status"] == "ok" and r["recipient"] == HQ
    assert [(x["door"], x["n"]) for x in r["letters"]] == [(DOOR_A, "001"), (DOOR_A, "003"), (DOOR_B, "002")]
    assert r["examined"] == 5 and r["more"] is False and "notice" in r
    assert "SECRET" not in result["content"][0]["text"]
    first = r["letters"][0]
    # 보낸 이는 봉투에 적힌 이름일 뿐이다. 창구는 서명을 확인하지 않고, 그렇다고 적는다
    assert first["sender"] == {"claimed": "lab:testonly-alpha", "verified": False}
    assert r["signatures_checked"] is False and "unverified" in r["notice"] and "from" not in first
    assert first["body_sha256"] == _sha(b"first for hq")
    assert r["complete"] is True and r["observed_at"] == "2026-10-09T00:00:00Z"
    assert first["event_id"] == _sha(_mailbox()[DOOR_A]["001"]["envelope"]) and "body" not in first
    # 남의 앞 편지는 한 통 받기에서도 없는 것과 같다. 본문이 새지 않는다
    result, r = _call(f, "mail_get", {"door": DOOR_A, "n": "002"})
    assert result["isError"] is True and r == {"error": "no such letter in this mailbox"}
    assert "SECRET" not in result["content"][0]["text"]
    assert _call(f, "mail_get", {"door": DOOR_A, "n": "099"})[1] == r        # 없는 편지와 같은 답
    # 다른 연구소의 우편함에서는 반대로 보인다
    g = _front(grants={"other": {"recipient": OTHER, "doors": [DOOR_A]}})
    assert [x["n"] for x in _call(g, "mail_list", caller="other")[1]["letters"]] == ["002"]
    # 우편함에 없는 문은 읽지 못한다
    h = _front(grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    assert [x["door"] for x in _call(h, "mail_list")[1]["letters"]] == [DOOR_A, DOOR_A]
    assert _call(h, "mail_list", {"door": DOOR_B})[0]["isError"] is True
    assert _call(h, "mail_get", {"door": DOOR_B, "n": "002"})[0]["isError"] is True
    # 받는 이를 읽을 수 없는 봉투는 지나간다
    box = _mailbox()
    box[DOOR_A]["004"] = {"envelope": b"not json", "sig": SIG, "body": None, "body_name": None}
    assert [x["n"] for x in _call(_front(box), "mail_list", {"door": DOOR_A})[1]["letters"]] == ["001", "003"]


def test_bytes_come_back_as_they_were_stored():
    box = _mailbox()
    box[DOOR_A]["004"] = _letter(HQ, b"\x00\xff\xfebinary", name="body.bin")
    box[DOOR_A]["005"] = _letter(HQ, None)
    box[DOOR_A]["006"] = _letter(HQ, b"tampered", digest="0" * 64)
    f = _front(box)
    result, r = _call(f, "mail_get", {"door": DOOR_A, "n": "003"})
    stored = box[DOOR_A]["003"]
    assert r["status"] == "ok" and r["body_encoding"] == "text" and r["body"] == "셋째 편지, 한글"
    assert r["envelope"].encode("utf-8") == stored["envelope"] and r["sig"] == SIG
    assert r["envelope_sha256"] == r["event_id"] == _sha(stored["envelope"])
    assert r["body_sha256"] == _sha(stored["body"]) and r["body_size"] == len(stored["body"])
    assert r["body_matches_envelope"] is True and r["body_name"] == "body.md" and "notice" in r
    assert r["sender"] == {"claimed": "lab:testonly-alpha", "verified": False}
    assert r["signature_checked"] is False and "from" not in r
    # base64를 청하면 바이트 그대로다
    r = _call(f, "mail_get", {"door": DOOR_A, "n": "003", "encoding": "base64"})[1]
    assert r["body_encoding"] == "base64" and base64.b64decode(r["body"]) == stored["body"]
    # UTF-8이 아닌 본문은 청하지 않아도 base64로 간다
    r = _call(f, "mail_get", {"door": DOOR_A, "n": "004"})[1]
    assert r["body_encoding"] == "base64" and base64.b64decode(r["body"]) == b"\x00\xff\xfebinary"
    # 본문이 없는 편지
    r = _call(f, "mail_get", {"door": DOOR_A, "n": "005"})[1]
    assert r["body"] is None and r["body_size"] is None and r["body_matches_envelope"] is True
    # 본문이 봉투의 지문과 다르면 그렇다고 적는다. 판정은 받는 연구소가 한다
    r = _call(f, "mail_get", {"door": DOOR_A, "n": "006"})[1]
    assert r["body_matches_envelope"] is False and r["body"] == "tampered"
    for bad in ({"door": DOOR_A}, {"door": DOOR_A, "n": "3"}, {"door": DOOR_A, "n": 3},
                {"door": DOOR_A, "n": "003", "encoding": "hex"}):
        assert _call(f, "mail_get", bad)[0]["isError"] is True


def test_a_letter_over_the_response_limit_is_described_not_sent():
    """한도는 본문이 아니라 보내는 응답 전체에 건다(Jdot HQ 10-09 · Orin 057). 도구의 글은 JSON-RPC
    응답에 문자열로 담기며 한 번 더 이스케이프된다 — 그 뒤의 바이트로 잰다."""
    limit = 20_000
    box = {DOOR_A: {"001": _letter(HQ, b"x" * 30_000), "002": _letter(HQ, b"y" * 100),
                    "003": _letter(HQ, b'"' * 6000), "004": _letter(HQ, b"z" * 6000)}}
    f = _front(box, response_max_bytes=limit, grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    for modern in (False, True):
        raw, _, r = _wire(f, "mail_get", {"door": DOOR_A, "n": "001"}, modern=modern)
        assert r["status"] == "too_large" and r["body"] is None and r["body_size"] == 30_000
        assert r["body_sha256"] == _sha(b"x" * 30_000) and r["response_max_bytes"] == limit
        assert r["envelope"].encode("utf-8") == box[DOOR_A]["001"]["envelope"]     # 봉투는 간다
        assert b"xxxx" not in raw and len(raw) <= limit
        raw, _, r = _wire(f, "mail_get", {"door": DOOR_A, "n": "002"}, modern=modern)
        assert r["status"] == "ok" and r["body"] == "y" * 100 and len(raw) <= limit
        # 따옴표 6,000바이트는 도구의 글에서 두 배, 보내는 응답에서 네 배다. 글로만 재면 한도 안이다
        raw, result, r = _wire(f, "mail_get", {"door": DOOR_A, "n": "003"}, modern=modern)
        assert r["status"] == "too_large" and r["body"] is None and len(raw) <= limit
        raw, _, r = _wire(f, "mail_get", {"door": DOOR_A, "n": "004"}, modern=modern)
        assert r["status"] == "ok" and r["body"] == "z" * 6000 and len(raw) <= limit
    # 기본 한도는 선행 시험에서 온전히 온 크기 안이다
    assert hf.RESPONSE_MAX_BYTES < 262144


def test_the_limit_is_measured_on_the_bytes_that_leave(tmp_path):
    """Orin 057의 재현 그대로: 기본 설정, 따옴표 100,000바이트 본문. 도구의 글은 201 KB라 한도 안이고
    보내는 응답은 401 KB였다."""
    box = {DOOR_A: {"001": _letter(HQ, b'"' * 100_000)}}
    f = _front(box, grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    for modern in (False, True):
        raw, _, r = _wire(f, "mail_get", {"door": DOOR_A, "n": "001"}, modern=modern)
        assert len(raw) <= hf.RESPONSE_MAX_BYTES and r["status"] == "too_large" and r["body"] is None
        assert r["body_size"] == 100_000 and r["body_sha256"] == _sha(b'"' * 100_000)
    # 한도는 정확하다. 딱 맞는 답은 가고, 한 바이트 모자라면 본문이 빠진다
    box = {DOOR_A: {"001": _letter(HQ, b'"\n' * 5000)}}
    grants = {"jj": {"recipient": HQ, "doors": [DOOR_A]}}
    for modern in (False, True):
        full = len(_wire(_front(box, response_max_bytes=10_000_000, grants=grants),
                         "mail_get", {"door": DOOR_A, "n": "001"}, modern=modern)[0])
        assert full > 30_000                              # 본문 10,000바이트가 세 배 넘게 부푼다
        raw, _, r = _wire(_front(box, response_max_bytes=full, grants=grants),
                          "mail_get", {"door": DOOR_A, "n": "001"}, modern=modern)
        assert r["status"] == "ok" and len(raw) == full
        raw, _, r = _wire(_front(box, response_max_bytes=full - 1, grants=grants),
                          "mail_get", {"door": DOOR_A, "n": "001"}, modern=modern)
        assert r["status"] == "too_large" and r["body"] is None and len(raw) < full
    # 무엇을 넣어도 넘지 않는다
    f = _front({DOOR_A: {f"{i:03d}": _letter(HQ, unit * size)
                         for i, (unit, size) in enumerate(
                             [(u, n) for u in (b'"', b"\n", b"\\", b"\x00", "한".encode(), b"x")
                              for n in (1000, 4000, 9000, 30_000)], start=1)}},
               response_max_bytes=hf.RESPONSE_MIN_BYTES, lookback=50, grants=grants)
    sizes = set()
    for modern in (False, True):
        for i in range(1, 25):
            for encoding in ("text", "base64"):
                raw, _, r = _wire(f, "mail_get", {"door": DOOR_A, "n": f"{i:03d}", "encoding": encoding},
                                  modern=modern)
                assert len(raw) <= hf.RESPONSE_MIN_BYTES, (i, encoding, len(raw))
                sizes.add(r["status"])
        assert len(_wire(f, "mail_list", {"limit": 50}, modern=modern)[0]) <= hf.RESPONSE_MIN_BYTES
    assert sizes == {"ok", "too_large"}


def test_when_the_envelope_alone_is_too_large_it_is_left_out_too():
    """봉투는 64 KiB까지다. 본문을 빼도 넘으면 봉투와 서명도 뺀다 — 지문과 크기는 남는다."""
    limit = 20_000
    q = _fat(HQ, b"small body", padding='"' * 5000)        # 봉투의 따옴표는 보내는 응답에서 여덟 배다
    records = []
    f = _front({DOOR_A: {"001": q}}, response_max_bytes=limit, audit=records.append,
               grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    for modern in (False, True):
        raw, result, r = _wire(f, "mail_get", {"door": DOOR_A, "n": "001"}, modern=modern)
        assert len(raw) <= limit and result["isError"] is False and r["status"] == "too_large"
        assert r["envelope"] is None and r["sig"] is None and r["body"] is None
        assert r["event_id"] == r["envelope_sha256"] == _sha(q["envelope"])
        assert r["body_size"] == 10 and r["body_sha256"] == _sha(b"small body")
        assert r["sender"] == {"claimed": "lab:testonly-alpha", "verified": False}
    assert [(x["status"], x["delivered"]) for x in records] == [("too_large", [[DOOR_A, "001"]])] * 2
    # 한도가 넉넉하면 봉투는 바이트 그대로 간다
    r = _call(_front({DOOR_A: {"001": q}}, grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}}),
              "mail_get", {"door": DOOR_A, "n": "001"})[1]
    assert r["status"] == "ok" and r["envelope"].encode("utf-8") == q["envelope"]


def test_a_list_never_exceeds_the_limit_and_never_skips_a_letter():
    """목록도 같은 한도 안이다(Orin 057과 같은 계약). 봉투의 긴 칸은 싣지 않고, 그래도 넘으면 개수를
    줄여 다시 부르라고 답한다. 커서는 그대로라 빠지는 편지가 없다."""
    grants = {"jj": {"recipient": HQ, "doors": [DOOR_A]}}
    # 봉투의 칸 하나가 터무니없이 길면 그 칸을 싣지 않는다. 편지는 목록에 남는다
    box = {DOOR_A: {f"{i:03d}": _fat(HQ, b"x", created='"' * 30_000, frm="lab:" + "a" * 300,
                                     media="m" * 201) for i in range(1, 21)}}
    box[DOOR_A]["020"]["body_name"] = "b" * 201             # 읽는 길이 준 이름도 같은 길이 안이어야 실린다
    for modern in (False, True):
        raw, _, r = _wire(_front(box, grants=grants), "mail_list", modern=modern)
        assert len(raw) <= hf.RESPONSE_MAX_BYTES and r["status"] == "ok" and len(r["letters"]) == 20
        assert all(x["created_at"] is None and x["sender"] == {"claimed": None, "verified": False}
                   and x["body_media_type"] is None for x in r["letters"])
        assert [x["body_name"] for x in r["letters"]] == ["body.md"] * 19 + [None]
    assert _call(_front(box, grants=grants), "mail_get", {"door": DOOR_A, "n": "020"})[1]["body_name"] is None
    assert _call(_front(box, grants=grants), "mail_get", {"door": DOOR_A, "n": "019"})[1]["body_name"] == "body.md"
    # 칸이 길이 안이어도 여럿이면 넘는다 — 그때는 줄여서 다시 부르라고 한다
    wide = {DOOR_A: {f"{i:03d}": _fat(HQ, b"x", created='"' * 200, frm='"' * 200, media='"' * 200)
                     for i in range(1, 51)}}
    records = []
    f = _front(wide, response_max_bytes=hf.RESPONSE_MIN_BYTES, lookback=50, grants=grants,
               audit=records.append)
    for modern in (False, True):
        raw, result, r = _wire(f, "mail_list", {"limit": 50}, modern=modern)
        assert len(raw) <= hf.RESPONSE_MIN_BYTES and result["isError"] is True
        assert "smaller limit" in r["error"] and "cursor" not in r and "letters" not in r
    assert [(x["status"], len(x["examined"]), x["delivered"]) for x in records] == [("too_large", 50, [])] * 2
    seen, cursor = [], None
    for _ in range(60):
        raw, result, r = _wire(f, "mail_list", {"limit": 3, **({"cursor": cursor} if cursor else {})})
        assert len(raw) <= hf.RESPONSE_MIN_BYTES and result["isError"] is False
        assert r["letters"][0]["sender"]["claimed"] == '"' * 200     # 길이 안의 칸은 그대로 간다
        seen += [x["n"] for x in r["letters"]]
        cursor = r["cursor"]
        if not r["more"]:
            break
    assert seen == [f"{i:03d}" for i in range(1, 51)]
    # 한 통씩은 가장 작은 한도에서도 언제나 들어간다 — 막혀서 못 지나가는 편지가 없다
    raw, result, r = _wire(f, "mail_list", {"limit": 1}, modern=True)
    assert result["isError"] is False and [x["n"] for x in r["letters"]] == ["001"]


def test_problem_reports_count_toward_the_limit():
    """어긋난 봉투의 알림도 `limit`에 센다. 문 하나가 통째로 어긋나도 답이 끝없이 길어지지 않는다."""
    class Lying(hf.MemoryReader):
        def index(self, door, since="000", *, timeout=None):
            page = super().index(door, since)
            for e in page["index"]:
                e["envelope_sha256"] = "f" * 64
            return page
    box = {DOOR_A: {f"{i:03d}": _letter(HQ, b"x") for i in range(1, 11)}}
    f = _front(reader=Lying(box), grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    r = _call(f, "mail_list", {"limit": 3})[1]
    assert r["letters"] == [] and [x["n"] for x in r["problems"]] == ["001", "002", "003"]
    assert r["more"] is True and r["complete"] is False and r["examined"] == 3
    told, cursor = [x["n"] for x in r["problems"]], r["cursor"]
    for _ in range(10):
        r = _call(f, "mail_list", {"limit": 3, "cursor": cursor})[1]
        told += [x["n"] for x in r.get("problems", [])]
        cursor = r["cursor"]
        if not r["more"]:
            break
    assert told == [f"{i:03d}" for i in range(1, 11)]


def test_the_last_gate_bounds_every_response():
    """도구의 답이 아닌 것도 넘지 않는다. 요청이 실어 보낸 것(id, 메서드의 이름)을 되돌려 적다가 넘치면
    작은 오류로 바뀐다. id가 커서 넘친 것이면 id 없이 답한다."""
    limit = hf.RESPONSE_MIN_BYTES
    records = []
    f = _front(response_max_bytes=limit, audit=records.append)

    def post(message, headers=None):
        headers = headers or {}
        st, _, raw = f.handle_http("POST", headers, json.dumps(message).encode(), "jj")
        assert len(raw) <= limit
        lowered = {k.lower(): v for k, v in headers.items()}
        assert f.handle(message, "jj", lowered) == (st, json.loads(raw))     # 두 입구가 같은 답을 낸다
        return st, json.loads(raw)
    st, out = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert st == 200 and len(out["result"]["tools"]) == 2            # 가장 작은 한도에도 도구의 목록은 들어간다
    assert post({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": dict(META)}},
                {"MCP-Protocol-Version": MODERN, "Mcp-Method": "server/discover"})[0] == 200
    name = "m" * 9000
    st, out = post({"jsonrpc": "2.0", "id": "i" * 9000, "method": name})
    assert st == 500 and out["error"] == {"code": hf.ERR_INTERNAL, "message": "response too large"}
    assert out["id"] == "i" * 9000                                    # id가 들어가면 id와 함께
    st, out = post({"jsonrpc": "2.0", "id": "i" * 20_000, "method": "tools/list"})
    assert st == 500 and out["error"]["code"] == hf.ERR_INTERNAL and "id" not in out
    # 도구를 부른 요청의 id가 자리를 다 차지하면 편지는 가지 않는다. 감사에는 내준 것이 없다고 남는다
    st, out = post({"jsonrpc": "2.0", "id": "i" * 16_000, "method": "tools/call",
                    "params": {"name": "mail_get", "arguments": {"door": DOOR_A, "n": "001"}}})
    assert st == 200 and out["result"]["isError"] is True and "first for hq" not in json.dumps(out)
    assert json.loads(out["result"]["content"][0]["text"]) == {"error": "this letter does not fit in one response"}
    st, out = post({"jsonrpc": "2.0", "id": "i" * 16_300, "method": "tools/call",
                    "params": {"name": "mail_get", "arguments": {"door": DOOR_A, "n": "001"}}})
    assert st == 500 and out["id"] == "i" * 16_300 and out["error"]["code"] == hf.ERR_INTERNAL
    assert len(records) == 4 and all(x["status"] == "too_large" and x["delivered"] == [] for x in records)
    with pytest.raises(ValueError):
        _front(response_max_bytes=hf.RESPONSE_MIN_BYTES - 1)


# ── 본문은 받는 이를 확인한 뒤에만 ──────────────────────────────────────────

class _Counting(hf.MemoryReader):
    """무엇을 읽었는지 적는 읽는 길."""

    def __init__(self, letters, many=False):
        super().__init__(letters)
        self.calls = []
        if many:
            self.envelopes = self._envelopes

    def envelope(self, door, n, *, timeout=None):
        self.calls.append(("envelope", door, n))
        return self.letters[door][n]["envelope"]

    def letter(self, door, n, *, timeout=None):
        self.calls.append(("letter", door, n))
        return super().letter(door, n)

    def _envelopes(self, door, since, limit, *, timeout=None):
        self.calls.append(("envelopes", door, since, limit))
        ns = [n for n in sorted(self.letters.get(door, {}), key=int) if int(n) > int(since)][:limit]
        return [(n, self.letters[door][n]["envelope"]) for n in ns]


def test_a_body_is_read_only_after_the_recipient_is_confirmed():
    """Orin 056. 남의 앞 편지의 본문은 호출자에게 가지 않을 뿐 아니라 창구가 가져오지도 않는다."""
    reader = _Counting(_mailbox())
    f = _front(reader=reader)
    _call(f, "mail_list")
    assert all(c[0] == "envelope" for c in reader.calls) and len(reader.calls) == 5   # 목록은 봉투만
    reader.calls.clear()
    assert _call(f, "mail_get", {"door": DOOR_A, "n": "002"})[0]["isError"] is True
    assert reader.calls == [("envelope", DOOR_A, "002")]             # 남의 앞: 본문을 가져오지 않았다
    reader.calls.clear()
    assert _call(f, "mail_get", {"door": DOOR_A, "n": "001"})[1]["body"] == "first for hq"
    assert reader.calls == [("envelope", DOOR_A, "001"), ("letter", DOOR_A, "001")]
    # 두 번 읽는 사이에 편지가 달라졌으면 내주지 않는다

    class Shifting(hf.MemoryReader):
        def envelope(self, door, n, *, timeout=None):
            return _letter(HQ, b"other")["envelope"]
    result, r = _call(_front(reader=Shifting(_mailbox())), "mail_get", {"door": DOOR_A, "n": "001"})
    assert result["isError"] is True and "changed" in r["error"] and "first for hq" not in json.dumps(r)


def test_envelopes_are_read_in_batches_when_the_reader_offers_it():
    """LxM 157 §4. 커서 없이 매시간 불려도 요청이 편지 수만큼 늘지 않는다."""
    box = {DOOR_A: {f"{i:03d}": _letter(HQ if i % 2 else OTHER, f"b{i}".encode()) for i in range(1, 46)}}
    reader = _Counting(box, many=True)
    f = _front(reader=reader, lookback=45, grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    r = _call(f, "mail_list", {"limit": 50})[1]
    assert [x["n"] for x in r["letters"]] == [f"{i:03d}" for i in range(1, 46, 2)] and r["examined"] == 45
    assert reader.calls == [("envelopes", DOOR_A, "000", 20), ("envelopes", DOOR_A, "020", 20),
                            ("envelopes", DOOR_A, "040", 20)]         # 봉투 마흔다섯에 요청 셋
    # 묶음에 없는 번호는 하나씩 읽는다(빈 번호 뒤, 그 사이에 생긴 편지)
    reader.calls.clear()
    reader.envelopes = lambda door, since, limit, *, timeout=None: []
    r = _call(f, "mail_list", {"limit": 2})[1]
    assert [x["n"] for x in r["letters"]] == ["001", "003"]
    assert [c[0] for c in reader.calls] == ["envelope", "envelope", "envelope"]


# ── 감사 ─────────────────────────────────────────────────────────────────────

def test_audit_hook_records_what_was_opened_and_what_was_delivered():
    """LxM 157 §5. 읽는 길이 운영자의 권한으로 돌 때 창구가 자기 감사를 남긴다 — 내용 없이."""
    records = []
    f = _front(audit=records.append)
    _call(f, "mail_list")
    every = [[DOOR_A, "001"], [DOOR_A, "002"], [DOOR_A, "003"], [DOOR_B, "001"], [DOOR_B, "002"]]
    assert records == [{"at": "2026-10-09T00:00:00Z", "caller": "jj", "tool": "mail_list",
                        "recipient": HQ, "status": "ok", "fetched": every, "examined": every,
                        "delivered": [[DOOR_A, "001"], [DOOR_A, "003"], [DOOR_B, "002"]]}]
    records.clear()
    _call(f, "mail_get", {"door": DOOR_A, "n": "003"})
    _call(f, "mail_get", {"door": DOOR_A, "n": "002"})                # 남의 앞: 열었고 내주지 않았다
    assert [(r["tool"], r["status"], r["fetched"], r["examined"], r["delivered"]) for r in records] == [
        ("mail_get", "ok", [[DOOR_A, "003"]], [[DOOR_A, "003"]], [[DOOR_A, "003"]]),
        ("mail_get", "refused", [[DOOR_A, "002"]], [[DOOR_A, "002"]], [])]
    text = json.dumps(records, ensure_ascii=False)
    assert "SECRET" not in text and "셋째" not in text and "envelope" not in text     # 내용이 없다
    # 읽다가 실패한 호출: 연 것은 적고, 내준 것은 없다고 적는다
    class Failing(hf.MemoryReader):
        def envelope(self, door, n, *, timeout=None):
            if (door, n) == (DOOR_A, "003"):
                raise hf.ReaderError("store broke")
            return super().envelope(door, n)
    records.clear()
    result, r = _call(_front(reader=Failing(_mailbox()), audit=records.append), "mail_list")
    assert result["isError"] is True and "letters" not in r
    assert [(x["status"], x["fetched"], x["examined"], x["delivered"]) for x in records] == [
        ("error", [[DOOR_A, "001"], [DOOR_A, "002"]], [[DOOR_A, "001"], [DOOR_A, "002"]], [])]
    # 아무것도 가져오지 않은 호출은 적지 않는다
    records.clear()
    _call(f, "mail_get", {"door": "hub-ops/from-nowhere", "n": "001"})
    _call(f, "mail_list", {"limit": 0})
    assert records == []
    # 묶어 읽으면 가져온 봉투가 연 봉투보다 많다. 둘을 따로 적는다(Orin 057)
    g = _front(reader=_Counting(_mailbox(), many=True), audit=records.append,
               grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    _call(g, "mail_list", {"limit": 1})
    assert [(x["fetched"], x["examined"], x["delivered"]) for x in records] == [
        ([[DOOR_A, "001"], [DOOR_A, "002"], [DOOR_A, "003"]], [[DOOR_A, "001"]], [[DOOR_A, "001"]])]


def test_when_the_audit_cannot_be_written_nothing_is_delivered():
    def broken(record):
        raise OSError("disk full")
    f = _front(audit=broken)
    result, r = _call(f, "mail_list")
    assert result["isError"] is True and "letters" not in r and "record" in r["error"]
    result, r = _call(f, "mail_get", {"door": DOOR_A, "n": "001"})
    assert result["isError"] is True and "first for hq" not in result["content"][0]["text"]
    # 감사 고리를 주지 않은 창구는 전과 같다
    assert _call(_front(), "mail_list")[1]["status"] == "ok"


# ── 커서 ─────────────────────────────────────────────────────────────────────

def test_cursor_resumes_and_does_not_widen():
    box = _mailbox()
    f = _front(box)
    r1 = _call(f, "mail_list")[1]
    assert len(r1["letters"]) == 3
    r2 = _call(f, "mail_list", {"cursor": r1["cursor"]})[1]
    assert r2["letters"] == [] and r2["examined"] == 0 and r2["cursor"] == r1["cursor"]
    box[DOOR_A]["004"] = _letter(OTHER, b"SECRET three")
    box[DOOR_A]["005"] = _letter(HQ, b"new one")
    r3 = _call(f, "mail_list", {"cursor": r1["cursor"]})[1]
    assert [(x["door"], x["n"]) for x in r3["letters"]] == [(DOOR_A, "005")] and r3["examined"] == 2
    assert _call(f, "mail_list", {"cursor": r3["cursor"]})[1]["letters"] == []
    # 개수를 줄이면 나눠서 온다. 빠지는 것도 겹치는 것도 없다
    seen, cursor = [], None
    for _ in range(10):
        r = _call(f, "mail_list", {"limit": 1, **({"cursor": cursor} if cursor else {})})[1]
        seen += [(x["door"], x["n"]) for x in r["letters"]]
        cursor = r["cursor"]
        if not r["more"]:
            break
    assert seen == [(DOOR_A, "001"), (DOOR_A, "003"), (DOOR_A, "005"), (DOOR_B, "002")]
    # 커서에 남의 문을 적어 넣어도 그 문은 열리지 않는다
    forged = base64.urlsafe_b64encode(json.dumps(
        {"v": 1, "p": {"letters/from-someone": "000", DOOR_A: "004"}}).encode()).decode().rstrip("=")
    g = _front(box, grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    r = _call(g, "mail_list", {"cursor": forged})[1]
    assert [(x["door"], x["n"]) for x in r["letters"]] == [(DOOR_A, "005")]
    assert "letters/from-someone" not in base64.urlsafe_b64decode(r["cursor"] + "==").decode()
    for bad in ("not-a-cursor", "", 12, base64.urlsafe_b64encode(b'{"v":2,"p":{}}').decode()):
        assert _call(f, "mail_list", {"cursor": bad})[0]["isError"] is True
    for bad in (0, hf.LIST_LIMIT_MAX + 1, "5", True):
        assert _call(f, "mail_list", {"limit": bad})[0]["isError"] is True


def test_first_call_looks_back_only_a_few_letters():
    box = {DOOR_A: {f"{i:03d}": _letter(HQ, f"letter {i}".encode()) for i in range(1, 31)}}
    f = _front(box, lookback=5, grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    r = _call(f, "mail_list")[1]
    assert [x["n"] for x in r["letters"]] == ["026", "027", "028", "029", "030"] and r["examined"] == 5
    assert _call(f, "mail_list", {"cursor": r["cursor"]})[1]["letters"] == []
    # 아무것도 못 본 채 끝나도 다음에 같은 자리에서 본다
    r0 = _call(f, "mail_list", {"limit": 2})[1]
    assert [x["n"] for x in r0["letters"]] == ["026", "027"] and r0["more"] is True
    assert [x["n"] for x in _call(f, "mail_list", {"cursor": r0["cursor"]})[1]["letters"]] == ["028", "029", "030"]


def test_index_pages_are_followed():
    class Paged(hf.MemoryReader):
        def index(self, door, since="000", *, timeout=None):
            full = super().index(door, since)["index"]
            return {"index": full[:4], "more": len(full) > 4}
    box = {DOOR_A: {f"{i:03d}": _letter(HQ, b"x") for i in range(1, 12)}}
    f = _front(reader=Paged(box), lookback=100, grants={"jj": {"recipient": HQ, "doors": [DOOR_A]}})
    assert [x["n"] for x in _call(f, "mail_list")[1]["letters"]] == [f"{i:03d}" for i in range(1, 12)]


def test_an_envelope_that_differs_from_the_index_is_reported_not_listed():
    class Lying(hf.MemoryReader):
        def index(self, door, since="000", *, timeout=None):
            page = super().index(door, since)
            for e in page["index"]:
                if e["n"] == "001":
                    e["envelope_sha256"] = "f" * 64
            return page
    r = _call(_front(reader=Lying(_mailbox())), "mail_list", {"door": DOOR_A})[1]
    assert [x["n"] for x in r["letters"]] == ["003"]
    assert r["problems"] == [{"door": DOOR_A, "n": "001", "problem": "envelope differs from the door index"}]


# ── 아직일 때 ────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_when_the_store_is_not_ready_the_tool_says_so_and_keeps_its_place():
    class Waking(hf.MemoryReader):
        fail_from = None
        fail_letter = False

        def envelope(self, door, n, *, timeout=None):
            if self.fail_from and (door, n) >= self.fail_from:
                raise hf.NotReady(41.6)
            return self.letters[door][n]["envelope"]

        def letter(self, door, n, *, timeout=None):
            if self.fail_letter:
                raise hf.NotReady(12)
            return super().letter(door, n)
    reader = Waking(_mailbox())
    f = _front(reader=reader)
    reader.fail_from = (DOOR_A, "001")
    result, r = _call(f, "mail_list")
    assert result["isError"] is False and r["status"] == "not_ready" and r["more"] is True
    assert r["letters"] == [] and r["retry_after_seconds"] == 42 and r["examined"] == 0
    assert r["complete"] is False                             # 「편지가 없다」가 아니라 「아직이다」
    # 일부를 본 뒤에 막히면 본 것은 내주고, 커서는 본 데까지 간다
    reader.fail_from = (DOOR_A, "003")
    r = _call(f, "mail_list")[1]
    assert r["status"] == "ok" and [x["n"] for x in r["letters"]] == ["001"] and r["more"] is True
    assert r["retry_after_seconds"] == 42
    reader.fail_from = None
    r2 = _call(f, "mail_list", {"cursor": r["cursor"]})[1]
    assert [(x["door"], x["n"]) for x in r2["letters"]] == [(DOOR_A, "003"), (DOOR_B, "002")]
    assert r2["more"] is False and "retry_after_seconds" not in r2
    reader.fail_letter = True
    result, r = _call(f, "mail_get", {"door": DOOR_A, "n": "001"})
    assert result["isError"] is False and r["status"] == "not_ready" and r["retry_after_seconds"] == 12
    assert "body" not in r


def test_the_time_budget_stops_the_scan_instead_of_hanging():
    clock = _Clock()

    class Slow(hf.MemoryReader):
        timeouts = []

        def envelope(self, door, n, *, timeout=None):
            self.timeouts.append(timeout)
            clock.t += 4.0
            return super().envelope(door, n)
    reader = Slow(_mailbox())
    f = _front(reader=reader, budget_seconds=6.0, clock=clock)
    r = _call(f, "mail_list")[1]
    # 4초짜리 읽기 둘이면 예산 6초를 넘긴다 — 둘째까지 보고, 같은 문의 셋째 앞에서 멈춘다
    assert r["examined"] == 2 and r["more"] is True and r["status"] == "ok"
    assert [x["n"] for x in r["letters"]] == ["001"]
    assert reader.timeouts == [6.0, 2.0]                      # 남은 시간만큼만 기다린다
    # 다시 부르면 이어서 본다. 빠지는 것이 없다
    seen, cursor = [(x["door"], x["n"]) for x in r["letters"]], r["cursor"]
    for _ in range(5):
        clock.t = 0.0
        r = _call(f, "mail_list", {"cursor": cursor})[1]
        seen += [(x["door"], x["n"]) for x in r["letters"]]
        cursor = r["cursor"]
        if not r["more"]:
            break
    assert seen == [(DOOR_A, "001"), (DOOR_A, "003"), (DOOR_B, "002")] and r["more"] is False


def test_reader_failures_are_tool_errors_without_details_of_the_store():
    class Broken(hf.MemoryReader):
        def index(self, door, since="000", *, timeout=None):
            raise hf.ReaderError("drop answered HTTP 403")
    result, r = _call(_front(reader=Broken(_mailbox())), "mail_list")
    assert result["isError"] is True and r == {"error": "mail store error: drop answered HTTP 403"}


def test_sample_reader_is_three_made_up_letters():
    reader = hf.sample_reader(HQ)
    door = "hub-ops/from-sample-sender"
    f = _front(reader=reader, grants={"jj": {"recipient": HQ, "doors": [door]}})
    r = _call(f, "mail_list")[1]
    assert [x["n"] for x in r["letters"]] == ["001", "003"] and r["examined"] == 3
    assert "한글" in _call(f, "mail_get", {"door": door, "n": "003"})[1]["body"]


# ── 드롭의 HTTP로 읽기 ───────────────────────────────────────────────────────

def _serve(tmp_path, lines, **kw):
    tok = tmp_path / "tokens.txt"
    tok.write_text("\n".join(lines) + "\n", encoding="utf-8")
    root = tmp_path / "drops"
    srv = hd.make_server(root, tok, bind="127.0.0.1", port=0, rate_limit_per_minute=0, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", root


def _place(root: Path, door: str, n: str, q: dict):
    d = root / door
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{n}-sig.txt").write_text(q["sig"] + "\n", encoding="utf-8")
    if q["body"] is not None:
        (d / f"{n}-{q['body_name']}").write_bytes(q["body"])
    (d / f"{n}-envelope.json").write_bytes(q["envelope"])


def _req(url, token, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_front_reads_a_real_drop_with_a_read_only_token(tmp_path):
    srv, url, root = _serve(tmp_path, ["tok-front  id=hq-mcp  read=hub-ops/*",
                                       "tok-narrow  id=narrow  read=hub-ops/from-beta"],
                            audit_dir=tmp_path / "audit")
    box = _mailbox()
    for door, letters in box.items():
        for n, q in letters.items():
            _place(root, door, n, q)
    reader = hf.DropReader(url, "tok-front", timeout=10)
    assert [e["n"] for e in reader.index(DOOR_A)["index"]] == ["001", "002", "003"]
    assert reader.index(DOOR_A, "002")["index"][0]["n"] == "003"
    assert reader.envelope(DOOR_A, "003") == box[DOOR_A]["003"]["envelope"]
    got = reader.letter(DOOR_A, "001")
    assert got == {"envelope": box[DOOR_A]["001"]["envelope"], "sig": SIG, "body": b"first for hq",
                   "body_name": "body.md"}
    with pytest.raises(LookupError):
        reader.letter(DOOR_A, "009")
    # 번호가 비어 있으면 그 뒤의 편지를 그 번호라고 내주지 않는다
    _place(root, "hub-ops/from-gamma", "005", _letter(HQ, b"five"))
    with pytest.raises(LookupError):
        reader.letter("hub-ops/from-gamma", "003")
    with pytest.raises(LookupError):
        reader.envelope("hub-ops/from-gamma", "003")
    # 봉투는 본문 없이, 스무 통까지 묶어 받는다
    assert reader.envelopes(DOOR_A, "000", 20) == [(n, box[DOOR_A][n]["envelope"]) for n in ("001", "002", "003")]
    assert reader.envelopes(DOOR_A, "001", 1) == [("002", box[DOOR_A]["002"]["envelope"])]
    f = _front(reader=reader)
    r = _call(f, "mail_list")[1]
    assert [(x["door"], x["n"]) for x in r["letters"]] == [(DOOR_A, "001"), (DOOR_A, "003"), (DOOR_B, "002")]
    assert r["letters"][1]["body_size"] == len("셋째 편지, 한글".encode("utf-8"))
    assert _call(f, "mail_get", {"door": DOOR_A, "n": "003"})[1]["body"] == "셋째 편지, 한글"
    # 창구의 읽기는 드롭의 감사 기록에 그 줄의 id로 남는다
    def audit_lines():
        return [json.loads(x) for p in sorted((tmp_path / "audit").glob("audit-*.jsonl"))
                for x in p.read_text(encoding="utf-8").splitlines()]
    lines = audit_lines()
    assert lines and {x["id"] for x in lines} == {"hq-mcp"} and all(x["method"] == "GET" for x in lines)
    # 목록 한 번은 문마다 색인 하나와 봉투 묶음 하나다. 본문 없이 읽었다고 적힌다
    before = len(lines)
    _call(f, "mail_list")
    new = audit_lines()[before:]
    assert [(x.get("op"), x["door"], x.get("bodies")) for x in new] == [
        ("index", "from-alpha", None), (None, "from-alpha", False),
        ("index", "from-beta", None), (None, "from-beta", False)]
    # 남의 앞 편지를 청하면 봉투만 읽고 끝난다
    before = len(audit_lines())
    assert _call(f, "mail_get", {"door": DOOR_A, "n": "002"})[0]["isError"] is True
    assert [x.get("bodies") for x in audit_lines()[before:]] == [False]
    # 드롭이 그 문을 닫으면 창구도 못 읽는다 — 좁힘은 드롭에 있다
    narrow = _front(reader=hf.DropReader(url, "tok-narrow", timeout=10))
    result, r = _call(narrow, "mail_list")
    assert result["isError"] is True and "403" in r["error"]
    srv.shutdown()
    # 닿지 않는 드롭은 「아직이다」다
    dead = _front(reader=hf.DropReader("http://127.0.0.1:9", "tok", timeout=1))
    r = _call(dead, "mail_list")[1]
    assert r["status"] == "not_ready" and r["retry_after_seconds"] >= 1


def test_drop_reader_needs_a_drop_that_knows_the_new_requests(tmp_path, monkeypatch):
    srv, url, root = _serve(tmp_path, ["tok-front  id=hq-mcp  read=hub-ops/*"])
    q = _mailbox()[DOOR_A]["001"]
    _place(root, DOOR_A, "001", q)
    reader = hf.DropReader(url, "tok-front", timeout=10)
    paths = []
    real = reader._get
    monkeypatch.setattr(reader, "_get", lambda path, timeout: (paths.append(path), real(path, timeout))[1])
    reader.envelope(DOOR_A, "001")
    reader.letter(DOOR_A, "001")
    assert paths == [f"{DOOR_A}?since=000&limit=1&bodies=0", f"{DOOR_A}?since=000&limit=1"]
    # 문 색인을 모르는 드롭(0.7.0)
    monkeypatch.setattr(reader, "_get", lambda path, timeout: {"quads": [], "more": False})
    with pytest.raises(hf.ReaderError, match="door index"):
        reader.index(DOOR_A)
    # 본문 없이 받기를 모르는 드롭(0.8.0)은 봉투에 본문을 딸려 보낸다 — 받지 않는다
    old = {"quads": [{"n": "001", "envelope_b64": base64.b64encode(q["envelope"]).decode(),
                      "sig": SIG, "body_name": "body.md",
                      "body_b64": base64.b64encode(q["body"]).decode()}], "more": False}
    monkeypatch.setattr(reader, "_get", lambda path, timeout: old)
    with pytest.raises(hf.ReaderError, match="0.9.0"):
        reader.envelope(DOOR_A, "001")
    srv.shutdown()


# ── 드롭 서버의 변경 셋 ──────────────────────────────────────────────────────

def test_drop_get_takes_a_limit(tmp_path):
    srv, url, root = _serve(tmp_path, ["tok-a  id=a"])
    for i in range(1, 6):
        _place(root, DOOR_A, f"{i:03d}", _letter(HQ, f"b{i}".encode()))
    st, body = _req(f"{url}/v0/{DOOR_A}?since=001&limit=1", "tok-a")
    assert st == 200 and [q["n"] for q in body["quads"]] == ["002"] and body["more"] is True
    st, body = _req(f"{url}/v0/{DOOR_A}?limit=5", "tok-a")
    assert [q["n"] for q in body["quads"]] == ["001", "002", "003", "004", "005"] and body["more"] is False
    st, body = _req(f"{url}/v0/{DOOR_A}?since=004&limit=1", "tok-a")
    assert [q["n"] for q in body["quads"]] == ["005"] and body["more"] is False
    assert len(_req(f"{url}/v0/{DOOR_A}", "tok-a")[1]["quads"]) == 5           # 없으면 전과 같다
    for bad in ("0", "21", "-1", "x", "1.5", ""):
        assert _req(f"{url}/v0/{DOOR_A}?limit={bad}", "tok-a")[0] == 400, bad
    # 본문 없이 받기: 봉투와 서명과 본문의 이름만 온다. 본문의 바이트는 없다
    st, body = _req(f"{url}/v0/{DOOR_A}?since=001&limit=2&bodies=0", "tok-a")
    assert st == 200 and [q["n"] for q in body["quads"]] == ["002", "003"] and body["more"] is True
    assert all("body_b64" not in q and q["body_name"] == "body.md" and q["sig"] == SIG
               for q in body["quads"])
    assert base64.b64decode(body["quads"][0]["envelope_b64"]) == _letter(HQ, b"b2")["envelope"]
    assert "body_b64" in _req(f"{url}/v0/{DOOR_A}?limit=1&bodies=1", "tok-a")[1]["quads"][0]
    for bad in ("2", "no", "", "00"):
        assert _req(f"{url}/v0/{DOOR_A}?bodies={bad}", "tok-a")[0] == 400, bad
    srv.shutdown()


def test_a_line_with_no_write_scope_cannot_write_the_state_slot(tmp_path, capsys):
    lines = ["tok-reader  id=hq-mcp  read=hub-ops/*",
             "tok-sender  id=hq  write=hub-ops/from-hq  read=hub-ops/*",
             "tok-compat  id=old",
             "tok-noid  read=hub-ops/*",
             "tok-gone  id=gone  write=hub-ops/from-gone  revoked"]
    srv, url, _ = _serve(tmp_path, lines, state_dir=tmp_path / "state")
    blob = b"bundle bytes"
    put = {"expect_generation": 0, "expect_sha256": "", "sha256": _sha(blob), "sig": SIG,
           "blob_b64": base64.b64encode(blob).decode()}
    st, body = _req(f"{url}/v0/state", "tok-reader", "POST", put)
    assert st == 403 and "쓰기 범위" in body["error"]
    assert not (tmp_path / "state" / "hq-mcp").exists()                       # 아무것도 쓰지 않았다
    assert _req(f"{url}/v0/state?meta=1", "tok-reader")[0] == 404              # 빈 칸을 읽는 것은 된다
    assert _req(f"{url}/v0/state", "tok-sender", "POST", put)[0] == 200
    assert _req(f"{url}/v0/state", "tok-compat", "POST", put)[0] == 200        # 호환 줄은 0.8.0과 같다
    assert _req(f"{url}/v0/state", "tok-noid", "POST", put)[0] == 403
    srv.shutdown()
    # check-tokens가 줄마다 상태 칸에서 할 수 있는 것을 찍는다
    tok = tmp_path / "tokens.txt"
    assert hub_cli.main(["check-tokens", "--token-file", str(tok)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(e["id"], e["state_slot"]) for e in out["entries"]][:3] == \
        [("hq-mcp", "read"), ("hq", "write"), ("old", "write")]
    assert [e["state_slot"] for e in out["entries"]][3:] == ["none", "none"]


def test_audit_line_for_a_state_read_carries_the_marker_answer(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    srv, url, _ = _serve(tmp_path, ["tok-sender  id=hq  write=hub-ops/from-hq"],
                         state_dir=tmp_path / "state", marks_dir=marks, audit_dir=tmp_path / "audit")
    blob = b"bundle bytes"
    put = {"expect_generation": 0, "expect_sha256": "", "sha256": _sha(blob), "sig": SIG,
           "blob_b64": base64.b64encode(blob).decode()}
    assert _req(f"{url}/v0/state", "tok-sender", "POST", put)[0] == 200
    assert _req(f"{url}/v0/state?meta=1", "tok-sender")[1]["mirrored"] is False
    (marks / "state" / "hq").mkdir(parents=True)
    (marks / "state" / "hq" / "00000001").write_bytes(b"")
    assert _req(f"{url}/v0/state?meta=1", "tok-sender")[1]["mirrored"] is True
    srv.shutdown()
    reads = [json.loads(x) for p in sorted((tmp_path / "audit").glob("audit-*.jsonl"))
             for x in p.read_text(encoding="utf-8").splitlines() if '"state_get"' in x]
    assert [x["mirrored"] for x in reads] == [False, True]
