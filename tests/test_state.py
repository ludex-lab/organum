"""상태 디렉터리 규율 — require_state_dir 가드 (조율 명령이 유령 .organum/을 못 만들게).

dogfood(warren)에서 발견: 조율 명령(agora join/roster me)이 init 없이 .organum/<field>만
mkdir하면 meta.json 없는 '반쪽 초기화'가 생겨 이후 context가 깨진다. 가드는 그 유령을
만들지도, 못 본 척 넘어가지도 않는다.
"""

import tempfile
import unittest
from pathlib import Path

from organum import state as st


class TestRequireStateDir(unittest.TestCase):
    def test_missing_state_dir_fails(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                st.require_state_dir(Path(td))

    def test_phantom_without_meta_fails(self):
        """meta.json 없는 조율 하위폴더만 있는 유령 → 조용히 통과하지 말고 실패."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".organum" / "agora").mkdir(parents=True)  # 유령
            with self.assertRaises(SystemExit):
                st.require_state_dir(Path(td))

    def test_initialized_returns_state_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir, _ = st.init_state_dir(root, "engine")
            self.assertEqual(st.require_state_dir(root), state_dir)

    def test_walks_up_from_subdir(self):
        """하위 폴더에서 실행해도 위로 올라가 리포 루트 .organum/을 쓴다 (하위 유령 방지)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir, _ = st.init_state_dir(root, "engine")
            deep = root / "src" / "warren"
            deep.mkdir(parents=True)
            self.assertEqual(st.require_state_dir(deep), state_dir)
            self.assertFalse((deep / ".organum").exists())  # 하위에 유령 없음


class TestSomaDir(unittest.TestCase):
    """soma/commons/field — 공존 세포별 개인 기관 (§2.3). owner=루트(v0 호환), 게스트=cells/<id>/."""

    def _init(self, td, agent="engine"):
        return st.init_state_dir(Path(td), agent)[0]

    def test_owner_and_none_resolve_to_root(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._init(td)
            self.assertEqual(st.soma_dir(sd), sd)            # 지정 없음 → owner → 루트
            self.assertEqual(st.soma_dir(sd, "engine"), sd)  # owner 명시 → 루트

    def test_guest_resolves_under_cells(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._init(td)
            self.assertEqual(st.soma_dir(sd, "atelier"), sd / "cells" / "atelier")

    def test_traversal_id_cannot_escape(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._init(td)
            for evil in ("..", ".", "../../etc"):
                d = st.soma_dir(sd, evil)
                self.assertEqual(d.parent, sd / "cells")     # 항상 cells/ 아래
                self.assertNotIn("..", d.name)

    def test_ensure_soma_scaffolds_guest_but_not_owner(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._init(td)
            d = st.ensure_soma(sd, "atelier")
            self.assertEqual(d, sd / "cells" / "atelier")
            self.assertTrue((d / "self.md").exists())
            self.assertTrue((d / "memory" / "events.jsonl").exists())
            self.assertTrue((d / "guard.jsonl").exists())
            # owner는 루트 그대로 — cells/ 안 만든다
            self.assertEqual(st.ensure_soma(sd, "engine"), sd)
            self.assertFalse((sd / "cells" / "engine").exists())


if __name__ == "__main__":
    unittest.main()
