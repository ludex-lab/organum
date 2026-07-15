"""organum mcp — MCP(stdio) JSON-RPC 서버로 조율 primitives 노출 (hand-rolled, stdlib-only)."""

import io
import json
import tempfile
import unittest
from pathlib import Path

from organum import mcp
from organum import state as st


def _run(cwd, cell, requests):
    inp = "\n".join(json.dumps(r) for r in requests) + "\n"
    out = io.StringIO()
    mcp.serve(cwd, cell, _in=io.StringIO(inp), _out=out)
    return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


class TestMcp(unittest.TestCase):
    def _init(self, td):
        st.init_state_dir(Path(td), "engine")
        return Path(td)

    def test_initialize_and_tools_list(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = self._init(td)
            resps = _run(cwd, "cellA", [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},  # 알림 → 무응답
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ])
            self.assertEqual(len(resps), 2)  # initialized는 응답 없음
            self.assertEqual(resps[0]["result"]["serverInfo"]["name"], "organum")
            self.assertEqual(resps[0]["result"]["protocolVersion"], "2024-11-05")
            names = {t["name"] for t in resps[1]["result"]["tools"]}
            self.assertEqual(names, {"agora_post", "agora_read", "relay_send", "relay_inbox",
                                     "alarm_sound", "alarm_active", "roster_me", "roster_read"})

    def test_post_then_cross_cell_read(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = self._init(td)
            a = _run(cwd, "cellA", [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "agora_post", "arguments": {"body": "hello from A"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "roster_me", "arguments": {"name": "lead", "focus": "test"}}},
            ])
            self.assertTrue(a[0]["result"]["content"][0]["text"].startswith("posted:"))
            self.assertTrue(a[1]["result"]["content"][0]["text"].startswith("presence:"))
            # 다른 세포가 토론장을 읽으면 A의 글이 보인다 (내 것 제외 규칙이 A엔 적용, B엔 안 됨)
            b = _run(cwd, "cellB", [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "agora_read", "arguments": {}}},
            ])
            self.assertIn("hello from A", b[0]["result"]["content"][0]["text"])
            # roster_read로 A의 presence가 보인다
            r = _run(cwd, "cellB", [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "roster_read", "arguments": {}}},
            ])
            self.assertIn("lead", r[0]["result"]["content"][0]["text"])

    def test_relay_directed(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = self._init(td)
            _run(cwd, "cellA", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "relay_send", "arguments": {"body": "to B only", "to": "cellB"}}}])
            b = _run(cwd, "cellB", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "relay_inbox", "arguments": {}}}])
            self.assertIn("to B only", b[0]["result"]["content"][0]["text"])

    def test_uninitialized_errors_cleanly(self):
        with tempfile.TemporaryDirectory() as td:  # init 안 함
            resps = _run(Path(td), "cellA", [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "roster_read", "arguments": {}}},
            ])
            self.assertTrue(resps[0]["result"].get("isError"))

    def test_unknown_method(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = self._init(td)
            resps = _run(cwd, "cellA", [
                {"jsonrpc": "2.0", "id": 9, "method": "no/such"},
            ])
            self.assertEqual(resps[0]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
