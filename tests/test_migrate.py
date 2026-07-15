"""migrate 정책 테스트 (§10). 합성 레지스트리로 V<N 경로까지 검증."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import FORMAT_VERSION, migrate, state as st


def _state(root: Path, version: int) -> Path:
    s = root / ".organum"
    (s / "memory").mkdir(parents=True)
    (s / "map").mkdir()
    (s / "worldmodel").mkdir()
    (s / "memory" / "events.jsonl").touch()
    (s / "guard.jsonl").touch()
    st.write_json(s / "meta.json", {
        "format_version": version, "organum_version": "0.0.1",
        "created_at": "2026-07-05T00:00:00Z", "project": "t", "agent": "t",
    })
    st.write_json(s / "map" / "repo.map.json", {
        "format_version": version, "seed_source": "none", "nodes": {}, "edges": [],
    })
    (s / "worldmodel" / "d.md").write_text(
        f"---\norganum-format: {version}\ndomain: d\nupdated: 2026-07-05T00:00:00Z\n---\n"
        "# WM: d\n\n## Map\n- a: b→c\n\n## Frontier\n- x\n\n## Claims\n- [tentative] y (evidence: z)\n",
        encoding="utf-8",
    )
    return s


class TestMigratePolicy(unittest.TestCase):
    def test_current_version_noop(self):
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td), FORMAT_VERSION)
            r = migrate.migrate(s)
            self.assertEqual(r["status"], "current")

    def test_future_version_refused(self):
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td), FORMAT_VERSION + 5)
            with self.assertRaises(migrate.MigrateError):
                migrate.migrate(s)

    def test_missing_step_refused(self):
        # target 2인데 0→1만 있고 1→2 결손 → 거부 (변환 시작 전에)
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td), 0)
            with self.assertRaises(migrate.MigrateError):
                migrate.migrate(s, target_version=2, registry={0: lambda sd: None},
                                backup_dir=Path(td) / "bk")

    def test_migration_applies_backup_bump_event(self):
        applied = []
        reg = {0: lambda sd: applied.append(sd)}
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td), 0)
            bk = Path(td) / "bk"
            r = migrate.migrate(s, target_version=1, registry=reg, backup_dir=bk)

            self.assertEqual(r["status"], "migrated")
            self.assertEqual((r["from"], r["to"]), (0, 1))
            self.assertEqual(len(applied), 1)  # 0→1 스텝 적용됨
            # 백업 생성됨 (변환 전 안전망)
            self.assertTrue(list(bk.glob("*.tar.gz")))
            # self-versioned 3종 동기 갱신 (§4)
            self.assertEqual(json.loads((s / "meta.json").read_text())["format_version"], 1)
            self.assertEqual(
                json.loads((s / "map" / "repo.map.json").read_text())["format_version"], 1
            )
            self.assertIn("organum-format: 1", (s / "worldmodel" / "d.md").read_text())
            # migrate 이벤트 기록
            events = [
                json.loads(l) for l in (s / "memory" / "events.jsonl").read_text().splitlines()
                if l.strip()
            ]
            self.assertTrue(any(e["kind"] == "migrate" for e in events))

    def test_backup_precedes_bump(self):
        # 백업 아카이브 안의 meta는 여전히 구버전이어야 한다 (변환 전 스냅샷)
        import tarfile
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td), 0)
            bk = Path(td) / "bk"
            migrate.migrate(s, target_version=1, registry={0: lambda sd: None}, backup_dir=bk)
            archive = list(bk.glob("*.tar.gz"))[0]
            with tarfile.open(archive) as tar:
                meta = json.load(tar.extractfile("meta.json"))
            self.assertEqual(meta["format_version"], 0)  # 백업은 변환 전 상태


if __name__ == "__main__":
    unittest.main()
