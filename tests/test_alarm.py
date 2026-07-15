"""alarm 경보 필드 — human/chief만 발동, 모두가 읽음. 정지는 세포 규율(강제 아님), 해제=human 보관."""

import tempfile
import unittest
from pathlib import Path

from organum import alarm, session
from organum import state as st


class TestAlarm(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, _ = st.init_state_dir(Path(self._tmp.name), "engine")
        self.cwd = self.state_dir.parent

    def tearDown(self):
        self._tmp.cleanup()

    def _open_chief(self, cell_id="watch01"):
        soma = st.ensure_soma(self.state_dir, cell_id)
        session.start(soma, cell_id, "chief", "세션 감시", "# chief\n")
        return cell_id

    def test_human_can_sound(self):
        fn = alarm.sound(self.cwd, self.state_dir, "seam 미검증 — 전체 주의", frm="human")
        self.assertIsNotNone(fn)
        a = alarm.active(self.cwd)[0]
        self.assertEqual((a["from"], a["level"]), ("human", "notice"))

    def test_chief_cell_can_sound_pause(self):
        cid = self._open_chief()
        fn = alarm.sound(self.cwd, self.state_dir, "scribe spiral — 정지 권고", frm=cid,
                         to="scribe01", level="pause")
        self.assertIsNotNone(fn)
        a = alarm.active(self.cwd)[0]
        self.assertEqual((a["from"], a["to"], a["level"]), (cid, "scribe01", "pause"))

    def test_worker_refused(self):
        soma = st.ensure_soma(self.state_dir, "worker01")
        session.start(soma, "worker01", "engine", "구현", "# engine\n")
        with self.assertRaises(alarm.AlarmError):
            alarm.sound(self.cwd, self.state_dir, "내가 울려본다", frm="worker01")

    def test_unknown_cell_refused_and_bad_level(self):
        with self.assertRaises(alarm.AlarmError):
            alarm.sound(self.cwd, self.state_dir, "x", frm="ghost99")
        with self.assertRaises(alarm.AlarmError):
            alarm.sound(self.cwd, self.state_dir, "x", frm="human", level="panic")

    def test_active_filters_addressed_and_resolved(self):
        alarm.sound(self.cwd, self.state_dir, "전체 주의", frm="human", to="all")
        f2 = alarm.sound(self.cwd, self.state_dir, "너만 정지", frm="human", to="scribe01",
                         level="pause")
        # scribe01에겐 둘 다, 남(engine02)에겐 all만 유효
        self.assertEqual(len(alarm.active(self.cwd, "scribe01")), 2)
        self.assertEqual(len(alarm.active(self.cwd, "engine02")), 1)
        # 해제(보관, 가역) — 활성에서 사라진다
        self.assertTrue(alarm.resolve(self.cwd, f2))
        self.assertEqual(len(alarm.active(self.cwd, "scribe01")), 1)

    def test_web_payload_and_archive_field(self):
        from organum import adapters, web
        fn = alarm.sound(self.cwd, self.state_dir, "정지 권고", frm="human", level="pause")
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0: []
        try:
            d = web.payload(self.cwd)
        finally:
            adapters.snapshot = orig
        self.assertEqual([(a["file"], a["level"]) for a in d["alarms"]], [(fn, "pause")])

    def test_mcp_alarm_tools(self):
        import io
        import json
        from organum import mcp

        def run(cell, name, arguments):
            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
            out = io.StringIO()
            mcp.serve(self.cwd, cell, _in=io.StringIO(json.dumps(req) + "\n"), _out=out)
            return json.loads(out.getvalue())["result"]["content"][0]["text"]

        cid = self._open_chief("mcpchief")
        self.assertIn("sounded:", run(cid, "alarm_sound",
                                      {"body": "정지 권고", "to": "worker01", "level": "pause"}))
        self.assertIn("거부", run("worker01", "alarm_sound", {"body": "무권한"}))
        self.assertIn("정지 권고", run("worker01", "alarm_active", {}))


if __name__ == "__main__":
    unittest.main()
