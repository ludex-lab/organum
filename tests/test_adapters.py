"""벤더 어댑터 — Codex 파서/발견 + snapshot 정규화 (실물 v0.144.1 스키마 기반 fixture)."""

import datetime
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from organum import adapters


def _codex_rollout(cwd, model="gpt-5.6-terra", branch="main", headless=True):
    ts = "2026-07-12T10:00:0"
    lines = [
        {"timestamp": ts + "0Z", "type": "session_meta", "payload": {
            "session_id": "019f5634-eae4-75f3-bff8-979e32cc7aa1", "id": "019f5634",
            "cwd": cwd, "originator": ("codex_exec" if headless else "codex-tui"),
            "source": ("exec" if headless else "cli"),
            "git": {"branch": branch, "commit_hash": "abc", "repository_url": "x"}}},
        {"timestamp": ts + "1Z", "type": "turn_context", "payload": {"model": model}},
        {"timestamp": ts + "2Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"}},
        {"timestamp": ts + "3Z", "type": "event_msg", "payload": {
            "type": "patch_apply_end", "changes": {"/repo/a.py": {}, "/repo/b.py": {}}, "success": True}},
        {"timestamp": ts + "4Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 100, "output_tokens": 40, "cached_input_tokens": 20, "total_tokens": 160}}}},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _codex_dir(root, days_ago=0):
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    dd = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
    dd.mkdir(parents=True, exist_ok=True)
    return dd


class TestCodexAdapterRead(unittest.TestCase):
    def test_read_maps_all_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout-x.jsonl"
            p.write_text(_codex_rollout("/repo", branch="dev"), encoding="utf-8")
            c = adapters.CodexAdapter().read(p)
            self.assertEqual(c["vendor"], "codex")
            self.assertEqual(c["model"], "gpt-5.6-terra")
            self.assertEqual((c["in_tok"], c["out_tok"], c["cache"]), (100, 40, 20))
            self.assertEqual(c["branch"], "dev")
            self.assertIn("shell", c["tools"])
            self.assertEqual(c["files"], ["/repo/a.py", "/repo/b.py"])
            self.assertEqual(c["origin"], "subagent")       # headless exec
            self.assertEqual(c["last_ts"], "2026-07-12T10:00:04Z")  # 마지막 내용 ts
            self.assertEqual(c["id"], "019f5634")

    def test_interactive_is_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout-y.jsonl"
            p.write_text(_codex_rollout("/repo", headless=False), encoding="utf-8")
            self.assertEqual(adapters.CodexAdapter().read(p)["origin"], "terminal")

    def test_first_ts_and_duration_source(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout-z.jsonl"
            p.write_text(_codex_rollout("/repo"), encoding="utf-8")
            c = adapters.CodexAdapter().read(p)
            self.assertEqual(c["first_ts"], "2026-07-12T10:00:00Z")
            self.assertEqual(c["last_ts"], "2026-07-12T10:00:04Z")

    def test_deep_reads_past_truncation(self):
        # 라이브 폴링은 head+tail 절삭(성능), 사후 계측(deep)은 전량 — 중간 툴 호출로 검증
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout-big.jsonl"
            mid_tool = json.dumps({"timestamp": "2026-07-12T10:00:02Z", "type": "response_item",
                                   "payload": {"type": "function_call", "name": "shell",
                                               "arguments": "{}", "call_id": "mid"}})
            filler = json.dumps({"timestamp": "2026-07-12T10:00:02Z", "type": "noise",
                                 "payload": {"pad": "x" * 200}})
            body = _codex_rollout("/repo").splitlines()
            # 중간 툴을 head 64KB·tail 2MB 절삭 바깥(파일 한가운데)에 심는다
            doc = "\n".join([body[0]] + [filler] * 22000 + [mid_tool] * 3
                            + [filler] * 23000 + body[1:]) + "\n"
            p.write_text(doc, encoding="utf-8")
            self.assertGreater(p.stat().st_size, 8_000_000)
            shallow = adapters.CodexAdapter().read(p)
            deep = adapters.CodexAdapter().read(p, deep=True)
            self.assertEqual(deep["tools"].get("shell"), 4)         # 중간 3 + 원본 1
            self.assertLess(shallow["tools"].get("shell", 0), 4)    # 절삭이 중간을 놓침
            self.assertEqual(deep["first_ts"], "2026-07-12T10:00:00Z")
            self.assertEqual(shallow["first_ts"], "2026-07-12T10:00:00Z")  # head에 line-1


class TestCodexAdapterDiscover(unittest.TestCase):
    def test_filters_by_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); dd = _codex_dir(root)
            (dd / "rollout-mine.jsonl").write_text(_codex_rollout("/want"), encoding="utf-8")
            (dd / "rollout-other.jsonl").write_text(_codex_rollout("/other"), encoding="utf-8")
            refs = adapters.CodexAdapter(root=root).discover("/want", window_min=60)
            self.assertEqual([r.name for r in refs], ["rollout-mine.jsonl"])

    def test_respects_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); dd = _codex_dir(root)
            old = dd / "rollout-old.jsonl"
            old.write_text(_codex_rollout("/want"), encoding="utf-8")
            past = time.time() - 3600
            os.utime(old, (past, past))
            self.assertEqual(adapters.CodexAdapter(root=root).discover("/want", window_min=30), [])

    def test_wide_window_reaches_old_date_dirs(self):
        # 광역 스윕(observatory)이 오늘/어제 너머 날짜 폴더도 봄 — warren Round One 누락 회귀
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); dd = _codex_dir(root, days_ago=3)
            old = dd / "rollout-r1.jsonl"
            old.write_text(_codex_rollout("/want"), encoding="utf-8")
            past = time.time() - 3 * 86400
            os.utime(old, (past, past))
            self.assertEqual(adapters.CodexAdapter(root=root).discover("/want", window_min=60), [])
            refs = adapters.CodexAdapter(root=root).discover("/want", window_min=7 * 24 * 60)
            self.assertEqual([r.name for r in refs], ["rollout-r1.jsonl"])


class TestSnapshot(unittest.TestCase):
    def test_normalizes_and_isolates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); dd = _codex_dir(root)
            (dd / "rollout-a.jsonl").write_text(_codex_rollout("/want"), encoding="utf-8")
            cells = adapters.snapshot("/want", window_min=60,
                                      adapters=[adapters.CodexAdapter(root=root)])
            self.assertEqual(len(cells), 1)
            self.assertEqual(cells[0]["vendor"], "codex")

    def test_survives_a_broken_adapter(self):
        class Boom(adapters.Adapter):
            name = "boom"
            def available(self):
                return True
            def discover(self, cwd, window_min=30.0):
                raise RuntimeError("boom")
        self.assertEqual(adapters.snapshot("/x", adapters=[Boom()]), [])


def _agy_dir(root, uuid="42616554-2857-42c7-8035-642b84d95edc"):
    logs = root / "brain" / uuid / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "transcript_full.jsonl", uuid


def _pb_tag(f, wt):
    return _pb_varint_enc((f << 3) | wt)


def _pb_varint_enc(x):
    out = b""
    while True:
        b7 = x & 0x7F
        x >>= 7
        out += bytes([b7 | (0x80 if x else 0)])
        if not x:
            return out


def _pb_msg(f, payload: bytes) -> bytes:
    return _pb_tag(f, 2) + _pb_varint_enc(len(payload)) + payload


def _pb_int(f, v) -> bytes:
    return _pb_tag(f, 0) + _pb_varint_enc(v)


def _agy_gen_blob(in_tok, out_tok, model="Test Model (X)"):
    """실물 gen_metadata 모양의 최소 blob: 1{ 4{2:in 3:out} 21:"model" }"""
    stats = _pb_int(2, in_tok) + _pb_int(3, out_tok)
    body = _pb_msg(4, stats) + _pb_msg(21, model.encode())
    return _pb_msg(1, body)


def _agy_sidecar(root, uuid, blobs):
    conv = root / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(conv / f"{uuid}.db")
    conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
    for i, b in enumerate(blobs):
        conn.execute("INSERT INTO gen_metadata VALUES (?,?,?)", (i, b, len(b)))
    conn.commit()
    conn.close()


def _agy_transcript(cwd):
    lines = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "status": "DONE", "created_at": "2026-07-12T10:00:00Z", "content": "go"},
        {"step_index": 1, "source": "MODEL", "type": "VIEW_FILE", "status": "DONE",
         "created_at": "2026-07-12T10:00:05Z",
         "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": cwd + "/a.py", "toolSummary": "x"}}]},
        {"step_index": 2, "source": "MODEL", "type": "RUN_COMMAND", "status": "DONE",
         "created_at": "2026-07-12T10:00:09Z",
         "tool_calls": [{"name": "run_command", "args": {"Cwd": cwd, "CommandLine": "ls"}}]},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


class TestAgyAdapter(unittest.TestCase):
    def test_read_tier1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tp, uuid = _agy_dir(root)
            tp.write_text(_agy_transcript("/repo"), encoding="utf-8")
            c = adapters.AgyAdapter(root=root).read(tp)
            self.assertEqual(c["vendor"], "agy")
            self.assertEqual(c["id"], uuid[:8])
            self.assertIsNone(c["model"])            # Tier-1: model/토큰 없음 (protobuf 후속)
            # 미측정은 0이 아니라 None — 관제탑이 '—'로 표시 (표시 정직성)
            self.assertIsNone(c["in_tok"])
            self.assertIsNone(c["out_tok"])
            self.assertIsNone(c["cache"])
            self.assertEqual(c["origin"], "terminal")
            self.assertIn("view_file", c["tools"])
            self.assertIn("run_command", c["tools"])
            self.assertIn("/repo/a.py", c["files"])
            self.assertEqual(c["last_ts"], "2026-07-12T10:00:09Z")

    def test_read_tier2_sidecar_tokens_and_model(self):
        # 사이드카 conversations/<uuid>.db의 protobuf에서 model·in/out — 요청별 합산, 마지막 모델
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tp, uuid = _agy_dir(root)
            tp.write_text(_agy_transcript("/repo"), encoding="utf-8")
            _agy_sidecar(root, uuid, [
                _agy_gen_blob(16768, 990, model="Gemini 3.5 Flash (Medium)"),
                _agy_gen_blob(17868, 94, model="Gemini 3.5 Flash (Medium)"),
            ])
            c = adapters.AgyAdapter(root=root).read(tp)
            self.assertEqual(c["model"], "Gemini 3.5 Flash (Medium)")
            self.assertEqual(c["in_tok"], 16768 + 17868)   # 요청별 입력 합산 (1.4.2)
            self.assertEqual(c["out_tok"], 990 + 94)       # 요청별 출력 합산 (1.4.3)
            self.assertIsNone(c["cache"])                  # cache 분해는 온디스크에 없음 → '—'

    def test_read_tier2_corrupt_blob_isolated(self):
        # 깨진 blob 하나가 나머지 관측을 막지 않는다 · DB 전체 실패는 Tier-1 폴백(None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tp, uuid = _agy_dir(root)
            tp.write_text(_agy_transcript("/repo"), encoding="utf-8")
            _agy_sidecar(root, uuid, [b"\xff\xff\xff", _agy_gen_blob(100, 40)])
            c = adapters.AgyAdapter(root=root).read(tp)
            self.assertEqual((c["in_tok"], c["out_tok"]), (100, 40))
            self.assertEqual(c["model"], "Test Model (X)")

    def test_discover_cwd_join(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tp1, u1 = _agy_dir(root, "11111111-aaaa-4ff3-8300-4ce98590345d")
            tp1.write_text(_agy_transcript("/want"), encoding="utf-8")
            tp2, u2 = _agy_dir(root, "22222222-bbbb-4ff3-8300-4ce98590345d")
            tp2.write_text(_agy_transcript("/elsewhere"), encoding="utf-8")
            refs = adapters.AgyAdapter(root=root).discover("/want", window_min=100000)
            self.assertEqual([r.parents[2].name for r in refs], [u1])


def _grok_session(sessions_root, cwd, sid="019f5915-7a7a-7011-8084-3587d6b01ed6",
                  model="grok-4.5", branch="main", parent=None):
    from urllib.parse import quote
    d = sessions_root / quote(str(Path(cwd)), safe="") / sid
    d.mkdir(parents=True, exist_ok=True)
    summary = {
        "info": {"id": sid, "cwd": cwd},
        "created_at": "2026-07-13T01:26:47.838552Z",
        "updated_at": "2026-07-13T01:26:52.074360Z",
        "last_active_at": "2026-07-13T01:26:52.074360Z",
        "num_messages": 8, "current_model_id": model, "head_branch": branch,
        "agent_name": "grok-build-plan", "reasoning_effort": "high",
    }
    if parent:
        summary["parent_session_id"] = parent
    (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (d / "signals.json").write_text(json.dumps({
        "contextTokensUsed": 13573, "contextWindowTokens": 500000,
        "toolCallCount": 1, "toolsUsed": ["read_file"], "primaryModelId": model,
    }), encoding="utf-8")
    updates = [
        {"method": "session/update", "params": {"update": {
            "sessionUpdate": "tool_call", "toolCallId": "c1", "title": "read_file",
            "rawInput": {"target_file": cwd + "/README.md"}}}},
        {"method": "session/update", "params": {"update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "c1", "kind": "read",
            "locations": [{"path": cwd + "/README.md"}]}}},
    ]
    (d / "updates.jsonl").write_text("\n".join(json.dumps(x) for x in updates) + "\n", encoding="utf-8")
    return d


class TestGrokAdapter(unittest.TestCase):
    def test_read_maps_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = _grok_session(root, "/repo", branch="dev")
            c = adapters.GrokAdapter(root=root).read(d)
            self.assertEqual(c["vendor"], "grok")
            self.assertEqual(c["model"], "grok-4.5")
            self.assertEqual(c["branch"], "dev")
            self.assertEqual(c["in_tok"], 13573)                # signals.contextTokensUsed
            self.assertIsNone(c["out_tok"])                     # grok은 out/cache를 안 남김 → 미측정(—)
            self.assertIsNone(c["cache"])
            self.assertEqual(c["tools"], {"read_file": 1})      # 초기 tool_call만 카운트
            self.assertEqual(c["files"], ["/repo/README.md"])   # locations[].path
            self.assertEqual(c["origin"], "terminal")
            self.assertEqual(c["last_ts"], "2026-07-13T01:26:52.074360Z")
            self.assertEqual(c["id"], "019f5915")

    def test_subagent_via_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = _grok_session(root, "/repo", sid="child-abc", parent="parent-xyz")
            self.assertEqual(adapters.GrokAdapter(root=root).read(d)["origin"], "subagent")

    def test_discover_filters_by_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _grok_session(root, "/want", sid="s-mine")
            _grok_session(root, "/other", sid="s-other")
            refs = adapters.GrokAdapter(root=root).discover("/want", window_min=60)
            self.assertEqual([r.name for r in refs], ["s-mine"])

    def test_respects_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = _grok_session(root, "/want")
            past = time.time() - 3600
            os.utime(d / "summary.json", (past, past))
            self.assertEqual(adapters.GrokAdapter(root=root).discover("/want", window_min=30), [])


def _opencode_db(root, cwd="/repo", sid="ses_test", model_id="solar-open2",
                 tokens=(8858, 73, 6432), updated_ms=None, archived=False, parent=None,
                 tool="read", filepath="/repo/README.md"):
    import sqlite3
    conn = sqlite3.connect(root / "opencode.db")
    conn.execute("CREATE TABLE IF NOT EXISTS session (id TEXT, directory TEXT, model TEXT, "
                 "parent_id TEXT, tokens_input INT, tokens_output INT, tokens_cache_read INT, "
                 "time_updated INT, time_archived INT)")
    conn.execute("CREATE TABLE IF NOT EXISTS part (message_id TEXT, session_id TEXT, data TEXT)")
    if updated_ms is None:
        updated_ms = int(time.time() * 1000)
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)", (
        sid, cwd, json.dumps({"id": model_id, "providerID": "upstage", "variant": "default"}),
        parent, tokens[0], tokens[1], tokens[2], updated_ms, (1 if archived else None)))
    conn.execute("INSERT INTO part VALUES (?,?,?)", ("msg1", sid, json.dumps(
        {"type": "tool", "tool": tool, "state": {"input": {"filePath": filepath}}})))
    conn.commit(); conn.close()


class TestOpenCodeAdapter(unittest.TestCase):
    def test_read_maps_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _opencode_db(root)
            c = adapters.OpenCodeAdapter(root=root).read("ses_test")
            self.assertEqual(c["vendor"], "opencode")
            self.assertEqual(c["model"], "solar-open2")            # model JSON blob → id
            self.assertEqual((c["in_tok"], c["out_tok"], c["cache"]), (8858, 73, 6432))
            self.assertEqual(c["tools"], {"read": 1})
            self.assertEqual(c["files"], ["/repo/README.md"])
            self.assertEqual(c["origin"], "terminal")
            self.assertTrue((c["last_ts"] or "").endswith("Z"))    # epoch ms → ISO

    def test_discover_filters_by_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _opencode_db(root, cwd="/want", sid="ses_mine")
            _opencode_db(root, cwd="/other", sid="ses_other")
            self.assertEqual(adapters.OpenCodeAdapter(root=root).discover("/want", window_min=60),
                             ["ses_mine"])

    def test_respects_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _opencode_db(root, cwd="/want", sid="ses_old",
                         updated_ms=int((time.time() - 3600) * 1000))
            self.assertEqual(adapters.OpenCodeAdapter(root=root).discover("/want", window_min=30), [])

    def test_subagent_via_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _opencode_db(root, sid="ses_child", parent="ses_parent")
            self.assertEqual(adapters.OpenCodeAdapter(root=root).read("ses_child")["origin"], "subagent")

    def test_archived_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _opencode_db(root, cwd="/want", sid="ses_arch", archived=True)
            self.assertEqual(adapters.OpenCodeAdapter(root=root).discover("/want", window_min=60), [])


def _claude_line(model="claude-fable-5", tool="Bash"):
    return json.dumps({"type": "assistant", "timestamp": "2026-07-12T10:00:00Z", "gitBranch": "main",
                       "message": {"model": model,
                                   "usage": {"input_tokens": 10, "output_tokens": 5,
                                             "cache_read_input_tokens": 3},
                                   "content": [{"type": "tool_use", "name": tool,
                                                "input": {"command": "ls"}}]}}) + "\n"


class TestClaudeAdapterSubagents(unittest.TestCase):
    """in-session 서브에이전트 = <부모세션uuid>/subagents/agent-*.jsonl — 발견·계보(parent)까지.
    (실측: 이 파일의 entrypoint는 'cli'라 sdk-cli 휴리스틱으로는 안 잡힌다 → 경로 판별.)"""

    def _tree(self, root, cwd="/want"):
        from organum import inspect as ins
        proj = root / str(Path(cwd)).replace("/", "-")
        proj.mkdir(parents=True)
        (proj / "aaaa1111-2222-3333.jsonl").write_text(_claude_line(), encoding="utf-8")
        sub = proj / "aaaa1111-2222-3333" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-deadbeef99.jsonl").write_text(
            _claude_line(model="claude-opus-4-8", tool="Read"), encoding="utf-8")
        return ins

    def test_discover_includes_subagents(self):
        with tempfile.TemporaryDirectory() as td:
            ins = self._tree(Path(td))
            orig = ins.CLAUDE_PROJECTS
            ins.CLAUDE_PROJECTS = Path(td)
            try:
                refs = adapters.ClaudeAdapter().discover("/want", window_min=60)
            finally:
                ins.CLAUDE_PROJECTS = orig
            self.assertEqual(sorted(p.name for p in refs),
                             ["aaaa1111-2222-3333.jsonl", "agent-deadbeef99.jsonl"])

    def test_read_subagent_sets_origin_and_parent(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(Path(td))
            p = Path(td) / "-want" / "aaaa1111-2222-3333" / "subagents" / "agent-deadbeef99.jsonl"
            c = adapters.ClaudeAdapter().read(p)
            self.assertEqual(c["origin"], "subagent")
            self.assertEqual(c["parent"], "aaaa1111")     # 경로에서 확정 — 휴리스틱 불요
            self.assertEqual(c["id"], "deadbeef")         # 'agent-' 접두 벗긴 id
            self.assertEqual(c["model"], "claude-opus-4-8")
            self.assertEqual((c["in_tok"], c["out_tok"], c["cache"]), (10, 5, 3))

    def test_read_terminal_has_no_parent(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(Path(td))
            c = adapters.ClaudeAdapter().read(Path(td) / "-want" / "aaaa1111-2222-3333.jsonl")
            self.assertEqual(c["origin"], "terminal")
            self.assertIsNone(c["parent"])


if __name__ == "__main__":
    unittest.main()
