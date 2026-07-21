"""organum mcp — MCP(stdio) 인터페이스로 조율 primitives를 노출한다.

`organum web`과 같은 결: 기존 조율 쓰기(relay/agora/roster)를 MCP 툴로 *노출*하는 인터페이스지,
LLM을 호출하거나 에이전트를 지휘하지 않는다(헌법: 관제탑, 관제사 아님). MCP 지원 에이전트
(OpenCode·Goose·Claude Code…)가 organum 조율을 native 툴로 쓰게 해 **조율을 벤더-일반화**한다 —
어댑터가 관측을 일반화하듯.

stdlib-only: JSON-RPC 2.0 (MCP stdio transport, 줄-구분) hand-roll. 한 서버 = 한 세포(`--for <id>`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from organum import __version__
from organum import agora as _agora
from organum import relay as _relay
from organum import roster as _roster
from organum import state as _state

_PROTOCOL = "2024-11-05"


def _tools() -> list:
    def obj(props, required=None):
        s = {"type": "object", "properties": props}
        if required:
            s["required"] = required
        return s
    return [
        {"name": "agora_post",
         "description": "토론장(개방 필드)에 게시 — 모두가 읽는다. 결정·핸드오프·리뷰에.",
         "inputSchema": obj({"body": {"type": "string", "description": "본문 (레코드당 한 주장)"},
                             "topic": {"type": "string"},
                             "thread": {"type": "string", "description": "스레드 id(대화 그룹)"},
                             "reply_to": {"type": "string", "description": "답장 대상 글 파일명(스레드 자동 상속)"},
                             "escalate": {"type": "boolean",
                                          "description": "human 개입 요청 — 관제탑 에스컬레이션 패널에 표시"}},
                            ["body"])},
        {"name": "agora_read",
         "description": "토론장의 안 읽은 새 글 (내 것 제외·가입 이후, 오래된 순).",
         "inputSchema": obj({"include_read": {"type": "boolean", "description": "읽은 것도 포함"}})},
        {"name": "relay_send",
         "description": "지향 편지 드롭 (특정 세포에게). to=대상 세포 id 또는 all.",
         "inputSchema": obj({"body": {"type": "string"}, "to": {"type": "string"},
                             "topic": {"type": "string"}, "thread": {"type": "string"},
                             "reply_to": {"type": "string"},
                             "escalate": {"type": "boolean",
                                          "description": "human 개입 요청 — 관제탑 에스컬레이션 패널에 표시"}},
                            ["body", "to"])},
        {"name": "relay_inbox",
         "description": "나에게 온 안 읽은 편지 (to=내 id/all, 내 편지 제외, 가입 이후).",
         "inputSchema": obj({"include_read": {"type": "boolean"}})},
        {"name": "alarm_sound",
         "description": "경보 발동 — human/chief(열린 세션)만. level=pause는 대상 세포 정지 권고 (강제 아님).",
         "inputSchema": obj({"body": {"type": "string", "description": "사유"},
                             "to": {"type": "string", "description": "대상 (all 또는 세포 id 콤마)"},
                             "level": {"type": "string", "enum": ["notice", "pause"]}},
                            ["body"])},
        {"name": "alarm_active",
         "description": "나에게 유효한 활성(미해제) 경보 — pause가 있으면 원자 작업만 마치고 정지+ACK (규율).",
         "inputSchema": obj({})},
        {"name": "roster_me",
         "description": "내 presence 선언/갱신 (부분 업데이트·heartbeat). 세션 시작 시 호출.",
         "inputSchema": obj({"name": {"type": "string"}, "focus": {"type": "string"},
                             "open_to": {"type": "array", "items": {"type": "string"}}})},
        {"name": "roster_read",
         "description": "현장의 선언된 세포 presence 목록 (누가 있나).",
         "inputSchema": obj({})},
    ]


def _base(cwd: Path) -> Path:
    # 조율 명령은 초기화된 .organum/의 부모(프로젝트 루트)를 cwd로 쓴다 (cli와 동일). 미초기화면 SystemExit.
    return _state.require_state_dir(cwd).parent


def _fmt(msgs: list) -> str:
    if not msgs:
        return "(없음)"
    out = []
    for m in msgs:
        head = f"— {m.get('from', '?')}"
        to = m.get("to")
        if to and to not in ("field", "all"):
            head += f" → {to}"
        if m.get("topic"):
            head += f" · {m['topic']}"
        if m.get("thread"):
            head += f" · thread:{str(m['thread'])[:12]}"
        out.append(f"{head} · {m.get('ts', '')}  [{m.get('file', '')}]\n{m.get('body', '')}")
    return "\n\n".join(out)


def _call(cwd: Path, cell_id: str, name: str, args: dict) -> str:
    base = _base(cwd)
    if name == "agora_post":
        fn = _agora.post(base, args.get("body", ""), frm=cell_id, from_id=cell_id,  # MCP 셀=canonical identity
                         topic=args.get("topic", "") or "",
                         src="mcp", thread=args.get("thread", "") or "", reply_to=args.get("reply_to", "") or "",
                         escalate=bool(args.get("escalate")))
        return f"posted: {fn}" if fn else "빈 본문 — 게시 안 됨"
    if name == "agora_read":
        return _fmt(_agora.read(base, cell_id, include_read=bool(args.get("include_read"))))
    if name == "relay_send":
        fn = _relay.send(base, args.get("body", ""), frm=cell_id, from_id=cell_id, to=args.get("to", "all"),
                         topic=args.get("topic", "") or "", src="mcp",
                         thread=args.get("thread", "") or "", reply_to=args.get("reply_to", "") or "",
                         escalate=bool(args.get("escalate")))
        return f"sent: {fn}" if fn else "빈 본문 — 전송 안 됨"
    if name == "relay_inbox":
        return _fmt(_relay.inbox(base, cell_id, include_read=bool(args.get("include_read"))))
    if name == "alarm_sound":
        from organum import alarm as _alarm
        try:
            fn = _alarm.sound(base, _state.require_state_dir(cwd), args.get("body", ""), frm=cell_id,
                              from_id=cell_id, to=args.get("to", "all") or "all",
                              level=args.get("level", "notice") or "notice", src="mcp")
        except _alarm.AlarmError as e:
            return f"거부: {e}"
        return f"sounded: {fn}" if fn else "빈 본문 — 발동 안 됨"
    if name == "alarm_active":
        from organum import alarm as _alarm
        alarms = _alarm.active(base, cell_id)
        if not alarms:
            return "(활성 경보 없음)"
        return "\n\n".join(
            f"⚠ [{a['level']}] {a['from']} → {a['to']} · {a.get('ts', '')}  [{a['file']}]\n{a['body']}"
            for a in alarms)
    if name == "roster_me":
        e = _roster.write_presence(base, cell_id, name=args.get("name"), focus=args.get("focus"),
                                   open_to=args.get("open_to"))
        return f"presence: {e.get('id')} · {e.get('name', '')} · focus: {e.get('focus', '')}"
    if name == "roster_read":
        cells = _roster.read_presence(base)
        if not cells:
            return "(선언된 세포 없음)"
        return "\n".join(
            f"— {c.get('id')} · {c.get('name', '?')} · focus: {c.get('focus', '')} · beat: {c.get('last_beat', '')}"
            for c in cells)
    raise ValueError(f"unknown tool: {name}")


def _send(out, id_, result):
    out.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}, ensure_ascii=False) + "\n")
    out.flush()


def _send_err(out, id_, code, msg):
    out.write(json.dumps({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}},
                         ensure_ascii=False) + "\n")
    out.flush()


def serve(cwd, cell_id: str, _in=None, _out=None) -> None:
    """MCP stdio 루프: stdin에서 JSON-RPC 요청을 읽어 조율 툴로 디스패치, stdout으로 응답."""
    cwd = Path(cwd)
    _in = _in if _in is not None else sys.stdin
    _out = _out if _out is not None else sys.stdout
    for line in _in:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        id_ = req.get("id")
        if method == "initialize":
            pv = (req.get("params") or {}).get("protocolVersion") or _PROTOCOL
            _send(_out, id_, {"protocolVersion": pv, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "organum", "version": __version__}})
        elif method == "notifications/initialized":
            continue  # 알림 — 응답 없음
        elif method == "tools/list":
            _send(_out, id_, {"tools": _tools()})
        elif method == "tools/call":
            p = req.get("params") or {}
            try:
                text = _call(cwd, cell_id, p.get("name", ""), p.get("arguments") or {})
                _send(_out, id_, {"content": [{"type": "text", "text": text}]})
            except SystemExit as e:  # require_state_dir 미초기화 등
                _send(_out, id_, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            except Exception as e:
                _send(_out, id_, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif method == "ping":
            _send(_out, id_, {})
        elif id_ is not None:
            _send_err(_out, id_, -32601, f"method not found: {method}")
