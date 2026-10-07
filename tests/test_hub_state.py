"""hub 상태의 저장·복원·대조·체크포인트(0.8.0) — 올리는 쪽.

설계: docs/hub-state-snapshot-restore-reconcile-v0-design.md §3–§8. 계약:
1. 묶음은 완결된 zlib 스트림 하나다. 옮기다 깨진 것, 모르는 조각, 맞지 않는 크기를 거부한다.
2. 저장 → 빈 자리에 복원 → 원장과 발신함이 바이트 단위로 같다. 다음 번호가 같다.
3. 불완전한 quad, 넣고 내보내지 않은 편지, bbs 발신 기록에서 저장을 거부한다.
4. 본문은 쓰기 전에 봉투의 지문과 대조한다.
5. 체크포인트는 200을 받고 표지를 본 뒤에 끝난다. 편지를 올리지 않는다.
6. 409는 자기 기록으로 가른다. 스스로 다시 올리는 것은 기록이 증명할 때뿐이다.
7. 복원은 남겨 둔 세대를 증인으로 본다. 잇지 않는 세대가 있으면 되살리지 않는다.
8. 자기 편지는 이 hub를 운영하는 연구소가 서명한 것이다. 등록해 둔 상대의 받은 편지는 세지 않는다.
9. 정상 체크포인트도 문과 번호 → 편지의 대응이 받아들여진 세대 그대로인지 본다.
10. 표지를 쓰지 않는 서버의 200은 올려도 된다는 답이 아니다. 200만 믿는 것은 이름 붙인 약한 모드다.

Jdot HQ의 실험(가짜 원장 39개·상태 칸 33개·v0.8 34개·v0.10 25개)을 출발점으로 삼았다. 그 실험은
시제품과 모형에 대한 것이었고 이것은 제품 함수에 대한 것이다. 실제 키와 실제 드롭은 쓰지 않는다 —
씨앗은 BIP340 시험 벡터의 공개된 값이다.
"""

import base64
import hashlib
import json
import shutil
import sys
import threading
import zlib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import hub_drop as hd          # noqa: E402
from organum import hub_envelope as he      # noqa: E402
from organum import hub_ops as ho           # noqa: E402
from organum import hub_state as hs         # noqa: E402
from organum import schnorr_pure as sp      # noqa: E402

SEED = (3).to_bytes(32, "big")              # TESTONLY — BIP340 시험 벡터의 공개된 값
PUB = sp.public_key(SEED).hex()
SEED2 = (4).to_bytes(32, "big")             # TESTONLY — 등록해 둔 상대 연구소의 키 노릇
PUB2 = sp.public_key(SEED2).hex()
DOOR = "hub-ops/from-testonly"
TOKEN = "tok-test"


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _hub(root: Path) -> tuple[Path, Path]:
    h, out = root / "hub", root / "out-hub-ops"
    h.mkdir(parents=True)
    out.mkdir()
    cfg = {"source_domain": "lab:testonly/hub", "machine_id": "TESTONLY",
           "claims": {"claims": {}}, "claims_sha256": he.canonical_sha({"claims": {}}),
           "keys": [{"pubkey": PUB, "signer_id": "lab:testonly", "key_id": "k1", "key_epoch": 1}]}
    (h / "hub.json").write_text(json.dumps(cfg), encoding="utf-8")
    (h / "events.jsonl").write_bytes(b"")
    return h, out


def _add(h: Path, out: Path, body: bytes | None, *, export=True, at="2026-10-06T00:00:00Z") -> dict:
    with ho.hub_writer(h) as (d, cfg, hub):
        env = ho.build_message_envelope(
            cfg, signer="lab:testonly", key_id="k1", epoch=1, to_lab="lab:receiver", to_id="R",
            to_epoch=1, body=body if body is not None else b"", body_locator="file://b",
            media_type="text/plain", created_at=at)
        r = ho.sign_and_admit(d, cfg, hub, env, SEED)
        assert r["admitted"]
        if not export:
            return r
        bp = None
        if body is not None:
            bp = h.parent / "body.md"
            bp.write_bytes(body)
        return ho.export_quad(d, out, event_id=r["event_id"], body_path=bp)


def _files(d: Path) -> dict:
    return {f.name: f.read_bytes() for f in sorted(d.iterdir())}


def _serve(tmp_path, *, marks=True, **kw):
    tok = tmp_path / "tokens.txt"
    tok.write_text(f"{TOKEN}  id=testonly\n", encoding="utf-8")
    mdir = tmp_path / "srv-marks" if marks else None
    if mdir:
        mdir.mkdir()
    srv = hd.make_server(tmp_path / "srv-drops", tok, bind="127.0.0.1", port=0,
                         rate_limit_per_minute=0, state_dir=tmp_path / "srv-state", marks_dir=mdir, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", mdir


def _mark_state(mdir: Path, gen: int):
    (mdir / "state" / "testonly").mkdir(parents=True, exist_ok=True)
    (mdir / "state" / "testonly" / f"{gen:08d}").write_bytes(b"")


def _mark_quad(mdir: Path, n: str):
    d = mdir / "quads" / "hub-ops" / "from-testonly"
    d.mkdir(parents=True, exist_ok=True)
    (d / n).write_bytes(b"")


def _push(url, out: Path, n: str):
    return hd.push_quad(f"{url}/v0/{DOOR}", TOKEN, out / n, warmup=False, allow_foreign_door=True)


def _cp(h, out, url, **kw):
    kw.setdefault("wait", 0)
    kw.setdefault("sleep", lambda s: None)
    return hs.checkpoint(h, {"out-hub-ops": out}, {"out-hub-ops": DOOR}, seed=SEED, drop_url=url,
                         token=TOKEN, **kw)


# ── 묶음 ─────────────────────────────────────────────────────────────────────

def _bundle():
    return hs.pack({"hub": {"events": 0}, "outboxes": []},
                   [("hub.json", b"{}"), ("events.jsonl", b""), ("body/out/001", b"\x00\xffbody")])


def test_pack_unpack_roundtrip_keeps_raw_bytes():
    m, parts = hs.unpack(_bundle())
    assert parts == {"hub.json": b"{}", "events.jsonl": b"", "body/out/001": b"\x00\xffbody"}
    assert [p["name"] for p in m["parts"]] == ["hub.json", "events.jsonl", "body/out/001"]


def test_unpack_accepts_only_one_complete_stream():
    blob = _bundle()
    for bad, word in ((blob[:-1], "잘렸다"), (blob + b"garbage", "붙어"),
                      (blob + zlib.compress(b"{}"), "붙어")):
        with pytest.raises(hs.StateError, match=word):
            hs.unpack(bad)
    # 느슨하게 풀면 뒤에 붙은 바이트를 모르고 지나간다 — 그래서 엄격히 본다
    assert zlib.decompress(blob + b"garbage") == zlib.decompress(blob)


def test_unpack_limits_exact_and_plus_one():
    blob = _bundle()
    hs.unpack(blob, max_bytes=len(blob))
    with pytest.raises(hs.StateError, match="한도"):
        hs.unpack(blob, max_bytes=len(blob) - 1)
    raw_len = len(zlib.decompress(blob))
    hs.unpack(blob, expanded_max=raw_len)
    with pytest.raises(hs.StateError, match="상한"):
        hs.unpack(blob, expanded_max=raw_len - 1)


def _repack(mutate):
    raw = zlib.decompress(_bundle())
    rest = raw[len(hs.MAGIC):]
    _sha_line, _, rest = rest.partition(b"\n")
    line, _, payload = rest.partition(b"\n")
    m = json.loads(line)
    payload = mutate(m, payload) or payload
    line = hs._canonical(m)
    return zlib.compress(hs.MAGIC + _sha(line).encode() + b"\n" + line + b"\n" + payload)


def test_unpack_rejects_malformed_bundles():
    def rename(m, p):
        m["parts"][2]["name"] = "../escape"
    def grow(m, p):
        return p + b"x"
    def wrong_size(m, p):
        m["parts"][2]["size"] += 1
    def wrong_sha(m, p):
        m["parts"][2]["sha256"] = "0" * 64
    def no_ledger(m, p):
        m["parts"] = m["parts"][2:]
        return p[2:]
    for mutate in (rename, grow, wrong_size, wrong_sha, no_ledger):
        with pytest.raises(hs.StateError):
            hs.unpack(_repack(mutate))
    raw = zlib.decompress(_bundle())
    with pytest.raises(hs.StateError, match="첫 줄"):
        hs.unpack(zlib.compress(b"X" + raw))
    with pytest.raises(hs.StateError, match="목록의 지문"):
        hs.unpack(zlib.compress(raw.replace(b'"events":0', b'"events":1')))
    dup = hs.MAGIC + _sha(b'{"a":1,"a":2}').encode() + b"\n" + b'{"a":1,"a":2}\n'
    with pytest.raises(hs.StateError, match="같은 키"):
        hs.unpack(zlib.compress(dup))


# ── 저장과 복원 ──────────────────────────────────────────────────────────────

def test_snapshot_restore_is_byte_identical(tmp_path):
    h, out = _hub(tmp_path / "a")
    _add(h, out, "한글\r\n".encode())
    _add(h, out, b"\x00\xff")
    _add(h, out, b"")
    # 본문 없는 quad. 빈 본문과 같은 봉투라 새 이벤트가 생기지 않는다 — 같은 이벤트가 새 번호로 한 번 더
    # 내보내진다(Jdot HQ 실험: 같은 이벤트를 다시 내보내면 새 번호가 붙는다). 번호 대응이 그대로 되살아나야 한다.
    _add(h, out, None)
    _, _, hub = ho.load_hub(h)
    before = ((h / "events.jsonl").read_bytes(), hub._log.root(), _files(out), ho.next_quad_number(out))
    blob = hs.snapshot(h, {"out-hub-ops": out}, doors={"out-hub-ops": DOOR})
    assert b"seed" not in zlib.decompress(blob)
    h2, out2 = tmp_path / "b" / "hub", tmp_path / "b" / "out"
    report = hs.restore(blob, h2, {"out-hub-ops": out2})
    _, _, hub2 = ho.load_hub(h2)
    after = ((h2 / "events.jsonl").read_bytes(), hub2._log.root(), _files(out2), ho.next_quad_number(out2))
    assert before == after and before[3] == "005"
    assert report["events"] == 3 and report["outboxes"]["out-hub-ops"]["quads"] == 4
    assert report["outboxes"]["out-hub-ops"]["bodies_to_fetch"] == []
    assert (out2 / "003-body.md").read_bytes() == b"" and not list(out2.glob("004-body.*"))
    assert report["outboxes"]["out-hub-ops"]["door"] == DOOR
    # 같은 번호와 같은 바이트의 재전송이 되살린 발신함에서도 선다
    assert (h2 / "hub.json").read_bytes() == (h / "hub.json").read_bytes()


def test_snapshot_refuses_incomplete_states(tmp_path):
    h, out = _hub(tmp_path)
    _add(h, out, b"one")
    (out / "002-sig.txt").write_bytes(b"leftover\n")              # 중단된 내보내기의 잔재
    with pytest.raises(hs.SnapshotRefused, match="불완전"):
        hs.snapshot(h, {"out-hub-ops": out})
    (out / "002-sig.txt").unlink()
    (out / "001.outbox.json").write_text("{}", encoding="utf-8")   # bbs의 발신 기록
    with pytest.raises(hs.SnapshotRefused, match="bbs"):
        hs.snapshot(h, {"out-hub-ops": out})
    (out / "001.outbox.json").unlink()
    r = _add(h, out, b"never exported", export=False)             # 넣고 내보내지 않은 편지
    with pytest.raises(hs.SnapshotRefused, match="어느 발신함에도 없다"):
        hs.snapshot(h, {"out-hub-ops": out})
    m, _ = hs.unpack(hs.snapshot(h, {"out-hub-ops": out}, allow_unexported=True))
    assert m["unexported"] == [r["event_id"]]


def test_received_letters_are_not_mistaken_for_own_unexported_letters(tmp_path):
    """Orin 053 A. `hub.json`의 keys에는 등록해 둔 상대 연구소의 검증 키도 있다. 그 연구소가 서명한 받은
    편지는 내 발신함에 없는 것이 맞다. 저장을 막는 것은 이 hub의 연구소가 서명하고 내보내지 않은 편지뿐이다."""
    h, out = _hub(tmp_path / "a")
    cfg = json.loads((h / "hub.json").read_text(encoding="utf-8"))
    cfg["keys"].append({"pubkey": PUB2, "signer_id": "lab:partner", "key_id": "k1", "key_epoch": 1})
    (h / "hub.json").write_text(json.dumps(cfg), encoding="utf-8")
    _add(h, out, b"mine, exported")
    with ho.hub_writer(h) as (d, cfg2, hub):                           # 상대가 서명한 편지 둘을 받는다
        for text in (b"theirs 1", b"theirs 2"):
            env = ho.build_message_envelope(
                cfg2, signer="lab:partner", key_id="k1", epoch=1, to_lab="lab:testonly", to_id="T",
                to_epoch=1, body=text, body_locator="file://b", media_type="text/plain",
                created_at="2026-10-06T00:00:00Z")
            raw = he.canonical_bytes(env)
            r = ho.admit_and_log(d, hub, raw, sp.sign(hashlib.sha256(raw).digest(), SEED2).hex(), PUB2)
            assert r["admitted"]
    m, _ = hs.unpack(hs.snapshot(h, {"out-hub-ops": out}))
    assert m["hub"]["events"] == 3 and m["unexported"] == []
    # 진짜 자기 미발신 편지가 하나 생기면 그 하나 때문에 거부한다. 받은 둘은 세지 않는다
    mine = _add(h, out, b"mine, not exported", export=False)
    with pytest.raises(hs.SnapshotRefused, match="lab:testonly.*1통"):
        hs.snapshot(h, {"out-hub-ops": out})
    m, _ = hs.unpack(hs.snapshot(h, {"out-hub-ops": out}, allow_unexported=True))
    assert m["unexported"] == [mine["event_id"]]
    # 연구소 이름을 맨이름으로 적은 hub: 같은 이름의 키가 hub.json에 있으면 그 연구소다
    assert hs._own_lab({"source_domain": "testonly/hub", "keys": cfg["keys"]}) == "lab:testonly"
    assert hs._own_lab({"source_domain": "partner/hub", "keys": cfg["keys"]}) == "lab:partner"
    # 정할 수 없는 hub는 저장하지 않는다 — 어느 편지가 자기 것인지 가릴 수 없다
    assert hs._own_lab({"source_domain": "organum-hub/local", "keys": cfg["keys"]}) is None
    cfg["source_domain"] = "organum-hub/local"
    (h / "hub.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(hs.SnapshotRefused, match="정할 수 없다"):
        hs.snapshot(h, {"out-hub-ops": out})


def test_snapshot_refuses_outbox_that_differs_from_the_ledger(tmp_path):
    h, out = _hub(tmp_path)
    _add(h, out, b"one")
    (out / "001-body.md").write_bytes(b"tampered")
    with pytest.raises(hs.SnapshotRefused, match="본문"):
        hs.snapshot(h, {"out-hub-ops": out})
    (out / "001-body.md").write_bytes(b"one")
    (out / "001-sig.txt").write_bytes(b"00" * 64 + b"\n")
    with pytest.raises(hs.SnapshotRefused, match="원장과 다르다"):
        hs.snapshot(h, {"out-hub-ops": out})


def test_restore_only_into_empty_places_and_checks_bodies_before_writing(tmp_path):
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    blob = hs.snapshot(h, {"out-hub-ops": out})
    with pytest.raises(hs.StateError, match="비어 있지 않다"):
        hs.restore(blob, h, {"out-hub-ops": tmp_path / "x"})
    with pytest.raises(hs.StateError, match="자리를 주지 않은"):
        hs.restore(blob, tmp_path / "y", {})
    # 본문 조각을 바꾸고 조각의 지문까지 다시 맞춘 묶음: 봉투의 지문과 달라서 쓰이지 않는다
    m, parts = hs.unpack(blob)
    parts["body/out-hub-ops/001"] = b"evil"
    del m["parts"]
    bad = hs.pack(m, list(parts.items()))
    h2, out2 = tmp_path / "b" / "hub", tmp_path / "b" / "out"
    with pytest.raises(hs.StateError, match="본문이 봉투의 지문과 다르다"):
        hs.restore(bad, h2, {"out-hub-ops": out2})
    assert not h2.exists() and not out2.exists()                  # 아무것도 남지 않는다


def test_omitted_bodies_are_reported_not_invented(tmp_path):
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    _add(h, out, b"two")
    blob = hs.snapshot(h, {"out-hub-ops": out}, omit_bodies={"out-hub-ops": {"001"}})
    _, parts = hs.unpack(blob)
    assert "body/out-hub-ops/001" not in parts and parts["body/out-hub-ops/002"] == b"two"
    h2, out2 = tmp_path / "b" / "hub", tmp_path / "b" / "out"
    report = hs.restore(blob, h2, {"out-hub-ops": out2})
    assert report["outboxes"]["out-hub-ops"]["bodies_to_fetch"] == ["001"]
    assert not (out2 / "001-body.md").exists() and (out2 / "001-envelope.json").is_file()
    assert ho.next_quad_number(out2) == "003"


# ── 대조 ─────────────────────────────────────────────────────────────────────

def test_reconcile_outcomes_and_exit_codes(tmp_path):
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    for b in (b"one", b"two", b"three"):
        _add(h, out, b)
    door_url = f"{url}/v0/{DOOR}"
    r = hs.reconcile(out, door_url, TOKEN)
    assert r["exit"] == 0 and set(r["outcomes"].values()) == {"only_local"}
    for n in ("001", "002", "003"):
        _push(url, out, n)
    assert hs.reconcile(out, door_url, TOKEN)["summary"] == {"in_sync": ["001", "002", "003"]}
    # 로컬이 낡았다: 드롭에만 있는 번호 — 낮은 번호가 빠진 경우도 처음부터 견주어 잡는다
    stale = tmp_path / "stale"
    shutil.copytree(out, stale)
    for f in stale.glob("002-*"):
        f.unlink()
    r = hs.reconcile(stale, door_url, TOKEN)
    assert r["outcomes"]["002"] == "only_remote" and r["exit"] == 1 and r["blocked"]
    # 같은 번호에 다른 바이트
    (stale / "003-body.md").write_bytes(b"different")
    r = hs.reconcile(stale, door_url, TOKEN)
    assert r["outcomes"]["003"] == "mismatch" and r["exit"] == 2
    # 봉투는 있는데 본문이 빠졌다
    (stale / "003-body.md").unlink()
    assert hs.reconcile(stale, door_url, TOKEN)["outcomes"]["003"] == "incomplete_local"
    srv.shutdown()


def test_reconcile_falls_back_when_the_server_has_no_index(tmp_path, monkeypatch):
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    _push(url, out, "001")
    monkeypatch.setattr(hs, "fetch_index", lambda *a, **k: None)      # 0.7.0 이하의 서버
    assert hs.reconcile(out, f"{url}/v0/{DOOR}", TOKEN)["summary"] == {"in_sync": ["001"]}
    srv.shutdown()


# ── 체크포인트 ───────────────────────────────────────────────────────────────

def test_checkpoint_waits_for_the_mark_and_does_not_push(tmp_path):
    srv, url, mdir = _serve(tmp_path)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    r = _cp(h, out, url)
    assert r["generation"] == 1 and r["mirrored"] is False and r["ready_to_push"] is False
    assert not (tmp_path / "srv-drops").exists()                    # 편지를 올리지 않았다
    rec = hs.load_record(h)
    assert rec["acked"][-1]["generation"] == 1 and rec["pending"] is None and rec["floor"] == 1
    # 감독기가 옮기고 표지를 쓴다. 기다리는 동안 표지가 서면 끝난다
    calls = []

    def sleep(_s):
        calls.append(1)
        _mark_state(mdir, 2)

    _add(h, out, b"two")
    r = _cp(h, out, url, wait=30, sleep=sleep)
    assert r["generation"] == 2 and r["mirrored"] is True and r["ready_to_push"] is True and calls
    srv.shutdown()


def test_server_without_marks_never_says_ready(tmp_path, capsys):
    """Orin 053 C. 표지를 쓰지 않는 서버의 200은 받았다는 뜻뿐이다. 올려도 된다고 답하지 않고, 그 서버의
    묶음으로 기본 복원을 하지 않는다. 200만 믿는 것은 이름 붙인 약한 모드이고 결과와 종료코드가 따로다."""
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    r = _cp(h, out, url)
    assert r["generation"] == 1 and r["mirrored"] is None
    assert r["ready_to_push"] is False and r["marks_absent"] is True and "received_only" not in r
    weak = _cp(h, out, url, received_only=True)
    assert weak["unchanged"] is True and weak["received_only"] is True
    assert weak["ready_to_push"] is False                             # 약한 모드도 준비됐다고는 하지 않는다

    with pytest.raises(hs.StateError, match="표지를 쓰지 않는다"):
        _restore(tmp_path, url, "r1")
    assert not (tmp_path / "r1" / "hub").exists()
    report = _restore(tmp_path, url, "r2", received_only=True)
    assert report["generation"] == 1 and report["received_only"] is True
    assert report["settled"] is None and report["mirrored"] is None

    # 명령줄: 0은 표지를 본 때뿐이다. 3은 표지 전, 4는 약한 모드
    seed = tmp_path / "test.seed"
    seed.write_text(SEED.hex() + "\n", encoding="utf-8")
    tok = tmp_path / "client-token.txt"
    tok.write_text(TOKEN + "\n", encoding="utf-8")
    base = ["checkpoint", "--dir", str(h), "--outbox", f"{out}={DOOR}", "--key", str(seed),
            "--url", url, "--token-file", str(tok), "--wait", "0"]
    code, r = _cli(capsys, *base)
    assert code == 3 and r["ready_to_push"] is False and r["marks_absent"] is True
    code, r = _cli(capsys, *base, "--received-only")
    assert code == 4 and r["received_only"] is True and r["ready_to_push"] is False
    restore = ["restore", "--url", url, "--token-file", str(tok), "--pubkey", PUB, "--wait", "0"]
    h3, out3 = tmp_path / "c" / "hub", tmp_path / "c" / "out"
    assert _cli(capsys, *restore, "--dir", str(h3), "--outbox", f"out-hub-ops={out3}")[0] == 2
    assert not h3.exists()
    code, r = _cli(capsys, *restore, "--dir", str(h3), "--outbox", f"out-hub-ops={out3}", "--received-only")
    assert code == 4 and r["received_only"] is True and _files(out3) == _files(out)
    srv.shutdown()


def test_received_only_does_not_weaken_a_server_that_writes_marks(tmp_path):
    srv, url, mdir = _serve(tmp_path)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    r = _cp(h, out, url, received_only=True)
    assert r["mirrored"] is False and r["ready_to_push"] is False and "received_only" not in r
    with pytest.raises(hs.StateError, match="자리를 잡지 않았다"):
        _restore(tmp_path, url, "r", received_only=True)
    (mdir / "settled").write_bytes(b"")
    with pytest.raises(hs.StateError, match="표지가 없다"):
        _restore(tmp_path, url, "r", received_only=True)
    _mark_state(mdir, 1)
    report = _restore(tmp_path, url, "r", received_only=True)
    assert report["mirrored"] is True and "received_only" not in report
    srv.shutdown()


def test_checkpoint_omits_only_bodies_that_are_marked_on_the_drop(tmp_path):
    srv, url, mdir = _serve(tmp_path)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    _add(h, out, b"two")
    _cp(h, out, url)
    _push(url, out, "001")
    _push(url, out, "002")
    _mark_quad(mdir, "001")                                         # 001만 바깥에 옮겨졌다
    _add(h, out, b"three")
    r = _cp(h, out, url)
    assert r["omitted_bodies"] == {"out-hub-ops": ["001"]}
    got = hs._http(f"{url}/v0/state", TOKEN)[1]
    _, parts = hs.unpack(base64.b64decode(got["blob_b64"]))
    assert sorted(k for k in parts if k.startswith("body/")) == [
        "body/out-hub-ops/002", "body/out-hub-ops/003"]            # 표지 없는 002는 드롭에 있어도 싣는다
    srv.shutdown()


def test_lost_response_resends_the_same_bytes(tmp_path, monkeypatch):
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    real = hs._http
    seen = []

    def lossy(u, token, data=None, timeout=10):
        st, body = real(u, token, data, timeout)
        if data is not None and not seen:
            seen.append(data["sha256"])
            raise TimeoutError("200을 잃었다")
        return st, body

    monkeypatch.setattr(hs, "_http", lossy)
    with pytest.raises(TimeoutError):
        _cp(h, out, url)
    rec = hs.load_record(h)
    assert rec["pending"]["generation"] == 1 and rec["floor"] == 1 and rec["acked"] == []
    # 기록이 없고(200을 못 받았다) 칸은 찼다 — 그래도 적어 둔 그 바이트를 같은 조건으로 다시 보낸다
    r = _cp(h, out, url, onto=(0, ""))
    assert r["generation"] == 1 and r["dedup"] is True and r["sha256"] == seen[0]
    srv.shutdown()


def test_resent_bytes_are_recorded_as_what_was_sent(tmp_path, monkeypatch):
    """Jdot HQ의 후보 시험 D1. 응답을 못 받은 뒤 같은 이벤트를 한 번 더 내보내면 원장은 그대로이고
    발신함만 달라진다. 다시 보낸 바이트는 002번을 모른다. 받았다는 기록이 002번을 안다고 적으면 안 되고,
    002번을 아는 세대가 올라간 뒤에야 올려도 된다고 답한다."""
    srv, url, mdir = _serve(tmp_path)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    real = hs._http
    seen = []

    def lossy(u, token, data=None, timeout=10):
        st, body = real(u, token, data, timeout)
        if data is not None and not seen:
            seen.append(data["sha256"])
            raise TimeoutError("200을 잃었다")
        return st, body

    monkeypatch.setattr(hs, "_http", lossy)
    with pytest.raises(TimeoutError):
        _cp(h, out, url)
    assert hs.load_record(h)["pending"]["next_n"] == {"out-hub-ops": "002"}
    events = (h / "events.jsonl").read_bytes()
    ho.export_quad(h, out, body_path=h.parent / "body.md")             # 같은 이벤트가 002번으로 한 번 더
    assert (h / "events.jsonl").read_bytes() == events and (out / "002-envelope.json").is_file()
    _mark_state(mdir, 1)                                               # 처음 보낸 세대는 바깥에 옮겨졌다

    r = _cp(h, out, url, onto=(0, ""))
    rec = hs.load_record(h)
    # 처음 보낸 바이트는 보낸 그대로 적힌다: 다음 번호 002
    first = rec["acked"][0]
    assert (first["generation"], first["sha256"]) == (1, seen[0])
    assert first["next_n"] == {"out-hub-ops": "002"}
    # 002번을 아는 세대가 그 위에 올라갔다. 그 세대의 표지는 아직 없으므로 올려도 된다고 답하지 않는다
    assert r["generation"] == 2 and rec["acked"][-1]["next_n"] == {"out-hub-ops": "003"}
    assert r["ready_to_push"] is False and r["extended_after_resend"] == 1
    m, _ = hs.unpack(base64.b64decode(real(f"{url}/v0/state", TOKEN)[1]["blob_b64"]))
    assert [i["n"] for i in m["outboxes"][0]["index"]] == ["001", "002"]
    assert m["outboxes"][0]["next_n"] == "003" and m["prev_sha256"] == seen[0]
    _mark_state(mdir, 2)
    again = _cp(h, out, url)
    assert again["unchanged"] is True and again["ready_to_push"] is True
    # 그 세대에서 되살리면 올린 002번이 발신함에 있다
    (mdir / "settled").write_bytes(b"")
    _restore(tmp_path, url, "cold")
    assert _files(tmp_path / "cold" / "out") == _files(out)
    srv.shutdown()


def test_rolled_back_slot_is_rebased_from_the_record_with_a_higher_number(tmp_path):
    """칸이 되돌아갔다(받아들여졌던 뒤쪽 쓰기가 보이지 않는다). 그 세대와 지문이 자기가 200을 받은
    기록에 있으면 그 위에 다시 올린다. 새 번호는 보낸 적 있는 번호보다 높다."""
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    for i, b in enumerate((b"one", b"two", b"three"), 1):
        _add(h, out, b)
        assert _cp(h, out, url)["generation"] == i
    slot = tmp_path / "srv-state" / "testonly"
    (slot / "00000003.state").unlink()
    (slot / "00000002.state").unlink()                               # 두 세대가 사라졌다
    _add(h, out, b"four")
    r = _cp(h, out, url)
    assert r["rebased_onto"] == 1 and r["generation"] == 5           # 4를 보내려 했으니 그보다 높다
    meta = hs._http(f"{url}/v0/state?meta=1", TOKEN)[1]
    assert meta["generation"] == 5 and meta["prev_generation"] == 1
    srv.shutdown()


def _place(tmp_path, gen, blob, prev):
    h = {"generation": gen, "sha256": _sha(blob), "prev_generation": prev[0], "prev_sha256": prev[1],
         "size": len(blob), "sig": sp.sign(hashlib.sha256(blob).digest(), SEED).hex()}
    d = tmp_path / "srv-state" / "testonly"
    d.mkdir(parents=True, exist_ok=True)
    assert hd._state_place(d, h, blob)
    return h["sha256"]


def test_409_without_evidence_stops_and_says_why(tmp_path):
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    assert _cp(h, out, url)["generation"] == 1
    base_sha = hs.load_record(h)["acked"][-1]["sha256"]
    other_root = tmp_path / "b"
    shutil.copytree(tmp_path / "a", other_root)
    hb, outb = other_root / "hub", other_root / "out-hub-ops"

    # 다른 주체가 내 상태를 이어받아 일을 더 했다 → 멈춘다. 새로 쓰지 않는다
    _add(hb, outb, b"by the other writer")
    theirs = hs.snapshot(hb, {"out-hub-ops": outb}, doors={"out-hub-ops": DOOR}, generation=2)
    _place(tmp_path, 2, theirs, (1, base_sha))
    with pytest.raises(hs.CheckpointStopped) as e:
        _cp(h, out, url)
    assert e.value.reason == "superseded"

    # 갈라졌다: 나도 그쪽이 모르는 일을 했다
    _add(h, out, b"mine")
    with pytest.raises(hs.CheckpointStopped) as e:
        _cp(h, out, url)
    assert e.value.reason == "diverged"
    assert hs._http(f"{url}/v0/state?meta=1", TOKEN)[1]["generation"] == 2       # 아무것도 올라가지 않았다
    srv.shutdown()


def test_no_record_and_a_filled_slot_never_uploads_on_its_own(tmp_path):
    """가장 새 세대의 번호만 묻고 자기 것을 올리면 낡은 사본이 새 상태를 밀어낸다."""
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    _cp(h, out, url)
    (h / hs.RECORD_FILE).unlink()                                     # 기록을 잃었다
    _add(h, out, b"two")
    with pytest.raises(hs.CheckpointStopped) as e:
        _cp(h, out, url)
    assert e.value.reason == "no_record_slot_not_empty" and e.value.detail["generation"] == 1
    cur = (e.value.detail["generation"], e.value.detail["sha256"])
    r = _cp(h, out, url, onto=cur)                                    # 사람이 견준 뒤 명시한다
    assert r["generation"] == 2
    m, _ = hs.unpack(base64.b64decode(hs._http(f"{url}/v0/state", TOKEN)[1]["blob_b64"]))
    assert m["supersedes"] == [cur[1]] and m["generation"] == 2 and m["prev_sha256"] == cur[1]
    srv.shutdown()


def test_checkpoint_refuses_a_state_that_went_backwards(tmp_path):
    srv, url, _ = _serve(tmp_path, marks=False)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    _add(h, out, b"two")
    _cp(h, out, url)
    lines = (h / "events.jsonl").read_bytes().splitlines(keepends=True)
    (h / "events.jsonl").write_bytes(lines[0])                        # 원장이 줄었다
    for f in out.glob("002-*"):
        f.unlink()
    with pytest.raises(hs.CheckpointStopped) as e:
        _cp(h, out, url)
    assert e.value.reason == "not_an_extension"
    srv.shutdown()


def _swap_first_two(out: Path):
    for f in list(out.glob("001-*")):
        f.rename(out / ("tmp" + f.name[3:]))
    for f in list(out.glob("002-*")):
        f.rename(out / ("001" + f.name[3:]))
    for f in list(out.glob("tmp-*")):
        f.rename(out / ("002" + f.name[3:]))


def test_checkpoint_refuses_renumbered_letters_and_a_changed_door(tmp_path):
    """Orin 053 B. 원장도 다음 번호도 그대로인데 번호가 가리키는 편지나 문이 바뀌었다. 정상 체크포인트가
    지난번에 받아들여진 세대의 표지를 보고 올려도 된다고 답하면 안 된다."""
    srv, url, mdir = _serve(tmp_path)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"first")
    _add(h, out, b"second")
    assert _cp(h, out, url)["generation"] == 1
    _mark_state(mdir, 1)
    assert _cp(h, out, url)["ready_to_push"] is True
    omap = hs.load_record(h)["acked"][-1]["outbox_map"]["out-hub-ops"]
    assert (omap["door"], omap["count"], omap["max_n"]) == (DOOR, 2, "002")
    before = hs._ledger_summary(h, {"out-hub-ops": out}, {"out-hub-ops": DOOR})

    _swap_first_two(out)                                              # 두 편지의 번호를 맞바꾼다
    after = hs._ledger_summary(h, {"out-hub-ops": out}, {"out-hub-ops": DOOR})
    assert all(after[k] == before[k] for k in ("events", "events_sha256", "next_n"))
    with pytest.raises(hs.CheckpointStopped) as e:
        _cp(h, out, url)
    assert e.value.reason == "not_an_extension" and "번호가 가리키는 편지" in str(e.value)
    _swap_first_two(out)                                              # 되돌리면 다시 선다
    assert _cp(h, out, url)["ready_to_push"] is True

    other = {"seed": SEED, "drop_url": url, "token": TOKEN, "wait": 0, "sleep": lambda s: None}
    with pytest.raises(hs.CheckpointStopped) as e:                    # 같은 발신함에 다른 문
        hs.checkpoint(h, {"out-hub-ops": out}, {"out-hub-ops": "hub-ops/from-other"}, **other)
    assert e.value.reason == "not_an_extension" and "문이" in str(e.value)
    spare = tmp_path / "a" / "out-spare"
    spare.mkdir()
    with pytest.raises(hs.CheckpointStopped) as e:                    # 지난번에 실은 발신함을 뺐다
        hs.checkpoint(h, {"spare": spare}, {"spare": "hub-ops/from-other"}, **other)
    assert e.value.reason == "not_an_extension"
    assert hs._http(f"{url}/v0/state?meta=1", TOKEN)[1]["generation"] == 1   # 아무것도 올라가지 않았다
    srv.shutdown()


def test_oversized_body_is_refused_before_it_enters_the_ledger(tmp_path):
    srv, url, _ = _serve(tmp_path, marks=False, state_max_bytes=4000)
    h, out = _hub(tmp_path / "a")
    assert hs.body_fits(h, b"x") is None                              # 상태 칸을 쓰지 않는 원장
    _add(h, out, b"one")
    _cp(h, out, url)
    assert hs.load_record(h)["max_bytes"] == 4000
    import os
    big = os.urandom(8000)                                            # 줄어들지 않는 본문
    fit = hs.body_fits(h, big)
    assert fit["fits"] is False and fit["max_bytes"] == 4000
    assert hs.body_fits(h, b"small")["fits"] is True
    before = (h / "events.jsonl").read_bytes()
    r = _cp(h, out, url, dry_run=True, with_body=big)
    assert r["dry_run"] and r["fits"] is False and (h / "events.jsonl").read_bytes() == before
    srv.shutdown()


# ── 상태 칸에서 되살리기 ─────────────────────────────────────────────────────

def _restore(tmp_path, url, name, **kw):
    kw.setdefault("wait", 0)
    kw.setdefault("sleep", lambda s: None)
    return hs.restore_from_slot(tmp_path / name / "hub", {"out-hub-ops": tmp_path / name / "out"},
                                drop_url=url, token=TOKEN, pubkey_hex=PUB, **kw)


def test_restore_needs_settled_and_mirrored_on_the_same_response(tmp_path):
    srv, url, mdir = _serve(tmp_path)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    _cp(h, out, url)
    with pytest.raises(hs.StateError, match="자리를 잡지 않았다"):
        _restore(tmp_path, url, "r1")
    (mdir / "settled").write_bytes(b"")
    with pytest.raises(hs.StateError, match="표지가 없다"):
        _restore(tmp_path, url, "r1")
    _mark_state(mdir, 1)
    report = _restore(tmp_path, url, "r1")
    assert report["generation"] == 1 and report["settled"] is True and report["mirrored"] is True
    assert _files(tmp_path / "r1" / "out") == _files(out)
    # 되살린 환경은 받은 세대와 지문을 기록의 첫 줄로 갖는다 — 다음 올리기가 그것을 조건으로 낸다
    rec = hs.load_record(tmp_path / "r1" / "hub")
    assert [a["generation"] for a in rec["acked"]] == [1] and rec["floor"] == 1
    # 시한으로만 자리를 잡은 인스턴스의 묶음은 기본으로 받지 않는다
    (mdir / "settled").unlink()
    (mdir / "settled-by-timeout").write_bytes(b"")
    with pytest.raises(hs.StateError, match="시한으로만"):
        _restore(tmp_path, url, "r2")
    assert _restore(tmp_path, url, "r2", accept_timeout=True)["generation"] == 1
    # 서명이 자기 공개키로 서지 않으면 받지 않는다
    with pytest.raises(hs.StateError, match="서명"):
        hs.restore_from_slot(tmp_path / "r3" / "hub", {"out-hub-ops": tmp_path / "r3" / "out"},
                             drop_url=url, token=TOKEN, pubkey_hex="11" * 32, accept_timeout=True,
                             wait=0, sleep=lambda s: None)
    srv.shutdown()


def test_kept_generations_are_witnesses_against_a_stale_higher_generation(tmp_path):
    """Jdot HQ의 v0.10 시험이 재현한 남는 경로. 옛 인스턴스의 높은 세대 9가 늦게 생겨 지금 세대가 된다.
    이어받은 쪽이 표지까지 받은 세대 2는 남아 있다. 세대 2는 세대 9의 앞부분이 아니다 — 멈춘다."""
    srv, url, mdir = _serve(tmp_path)
    (mdir / "settled").write_bytes(b"")
    base_root = tmp_path / "base"
    h, out = _hub(base_root)
    boxes = lambda root: {"out-hub-ops": root / "out-hub-ops"}        # noqa: E731
    doors = {"out-hub-ops": DOOR}
    g1 = hs.snapshot(h, boxes(base_root), doors=doors, generation=1)
    sha1 = _place(tmp_path, 1, g1, (0, ""))
    for name in ("old-instance", "successor"):
        shutil.copytree(base_root, tmp_path / name)
    _add(tmp_path / "successor" / "hub", tmp_path / "successor" / "out-hub-ops", b"new-body")
    _add(tmp_path / "old-instance" / "hub", tmp_path / "old-instance" / "out-hub-ops", b"old-body")
    g2 = hs.snapshot(tmp_path / "successor" / "hub", boxes(tmp_path / "successor"), doors=doors, generation=2)
    g9 = hs.snapshot(tmp_path / "old-instance" / "hub", boxes(tmp_path / "old-instance"), doors=doors,
                     generation=9)
    sha2 = _place(tmp_path, 2, g2, (1, sha1))
    sha9 = _place(tmp_path, 9, g9, (1, sha1))
    for g in (1, 2, 9):
        _mark_state(mdir, g)
    with pytest.raises(hs.StateError, match="갈라졌다"):
        _restore(tmp_path, url, "cold")
    assert not (tmp_path / "cold" / "hub").exists()
    # 사람이 이을 세대를 고른다. 그 세대에서 되살리고, 지금 세대 위에 명시해서 올린다
    report = _restore(tmp_path, url, "chosen", generation=2)
    assert report["generation"] == 2
    hc, outc = tmp_path / "chosen" / "hub", tmp_path / "chosen" / "out"
    assert (outc / "001-body.md").read_bytes() == b"new-body"
    r = hs.checkpoint(hc, {"out-hub-ops": outc}, doors, seed=SEED, drop_url=url, token=TOKEN,
                      onto=(9, sha9), wait=0, sleep=lambda s: None)
    assert r["generation"] == 10                                      # 덮인 세대보다 높다
    _mark_state(mdir, 10)
    # 덮은 묶음이 덮인 세대의 지문을 적어 두었으므로 다음 복원은 멈추지 않는다
    assert _restore(tmp_path, url, "after")["generation"] == 10
    assert sha2 != sha9
    srv.shutdown()


def test_same_ledger_with_renumbered_letters_is_not_an_extension(tmp_path):
    """원장이 그대로 이어져도 문의 번호가 다른 편지를 가리키면 잇는 것이 아니다. 드롭의 001번은 이미
    한 편지의 것이다."""
    srv, url, mdir = _serve(tmp_path)
    (mdir / "settled").write_bytes(b"")
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"first")
    _add(h, out, b"second")
    doors = {"out-hub-ops": DOOR}
    sha1 = _place(tmp_path, 1, hs.snapshot(h, {"out-hub-ops": out}, doors=doors, generation=1), (0, ""))
    shutil.copytree(tmp_path / "a", tmp_path / "b")
    hb, outb = tmp_path / "b" / "hub", tmp_path / "b" / "out-hub-ops"
    for f in list(outb.glob("001-*")):                                # 두 편지의 번호를 맞바꾼다
        f.rename(outb / ("tmp" + f.name[3:]))
    for f in list(outb.glob("002-*")):
        f.rename(outb / ("001" + f.name[3:]))
    for f in list(outb.glob("tmp-*")):
        f.rename(outb / ("002" + f.name[3:]))
    _add(hb, outb, b"third")
    _place(tmp_path, 2, hs.snapshot(hb, {"out-hub-ops": outb}, doors=doors, generation=2), (1, sha1))
    _mark_state(mdir, 1)
    _mark_state(mdir, 2)
    with pytest.raises(hs.StateError, match="갈라졌다"):
        _restore(tmp_path, url, "cold")
    srv.shutdown()


def test_unmarked_kept_generation_is_not_a_witness(tmp_path):
    srv, url, mdir = _serve(tmp_path)
    (mdir / "settled").write_bytes(b"")
    h, out = _hub(tmp_path / "a")
    g1 = hs.snapshot(h, {"out-hub-ops": out}, generation=1)
    sha1 = _place(tmp_path, 1, g1, (0, ""))
    shutil.copytree(tmp_path / "a", tmp_path / "b")
    _add(h, out, b"mine")
    _add(tmp_path / "b" / "hub", tmp_path / "b" / "out-hub-ops", b"theirs")
    _place(tmp_path, 2, hs.snapshot(tmp_path / "b" / "hub", {"out-hub-ops": tmp_path / "b" / "out-hub-ops"},
                                    generation=2), (1, sha1))
    _place(tmp_path, 3, hs.snapshot(h, {"out-hub-ops": out}, generation=3), (1, sha1))
    _mark_state(mdir, 1)
    _mark_state(mdir, 3)                                              # 세대 2에는 표지가 선 적이 없다
    assert _restore(tmp_path, url, "r")["witnesses"] == [1]
    srv.shutdown()


def test_rate_limit_is_waited_out_not_treated_as_failure(tmp_path, monkeypatch):
    """LxM 134 §5. 자리를 잡기를 기다리는 복원이 429에서 끝났다. 빈도 한도는 그만둘 까닭이 아니다 —
    서버가 말한 만큼 쉬고 이어 간다."""
    # 서버의 429는 Retry-After를 싣고, 올리는 쪽은 그 값을 읽는다
    tok = tmp_path / "limited-tokens.txt"
    tok.write_text(f"{TOKEN}  id=testonly\n", encoding="utf-8")
    limited = hd.make_server(tmp_path / "limited-drops", tok, bind="127.0.0.1", port=0,
                             rate_limit_per_minute=1, state_dir=tmp_path / "limited-state")
    threading.Thread(target=limited.serve_forever, daemon=True).start()
    lurl = f"http://127.0.0.1:{limited.server_address[1]}/v0/state?meta=1"
    assert hs._http(lurl, TOKEN)[0] == 404
    st, body = hs._http(lurl, TOKEN)
    assert st == 429 and 1 <= body["retry_after"] <= 60
    limited.shutdown()

    srv, url, mdir = _serve(tmp_path)
    (mdir / "settled").write_bytes(b"")
    _mark_state(mdir, 1)                                              # 표지는 미리 놓아 둔다
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    real = hs._http
    state = {"posted": False, "limited": 0}

    def limited_once(u, token, data=None, timeout=10):
        if data is not None:
            state["posted"] = True
        elif state["posted"] and state["limited"] == 0:
            state["limited"] = 1
            return 429, {"error": "rate limit", "retry_after": 9}
        return real(u, token, data, timeout)

    monkeypatch.setattr(hs, "_http", limited_once)
    sleeps = []
    r = _cp(h, out, url, wait=30, sleep=sleeps.append)
    assert r["ready_to_push"] is True and sleeps == [9]              # 표지 기다림: 9초 쉬고 이어 봤다

    state.update(limited=0)
    sleeps.clear()
    report = _restore(tmp_path, url, "cold", wait=30, sleep=sleeps.append)
    assert report["generation"] == 1 and sleeps == [9]               # 복원: 끝내지 않고 이어 갔다
    # 시한이 없으면 기다리지 않는다 — 되살리지 않고 끝난다
    state.update(limited=0)
    with pytest.raises(hs.StateError, match="HTTP 429"):
        _restore(tmp_path, url, "cold2")
    srv.shutdown()


def test_checkpoint_called_again_resumes_waiting_instead_of_uploading(tmp_path):
    """표지를 기다리다 끝난 뒤 같은 명령을 다시 부르면 이어서 본다. 달라진 것이 없으면 다시 뜨지 않는다."""
    srv, url, mdir = _serve(tmp_path)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    assert _cp(h, out, url)["ready_to_push"] is False
    r = _cp(h, out, url)
    assert r["generation"] == 1 and r.get("unchanged") is True and r["ready_to_push"] is False
    assert hs._http(f"{url}/v0/state?meta=1", TOKEN)[1]["generation"] == 1      # 세대가 오르지 않았다
    _mark_state(mdir, 1)
    assert _cp(h, out, url)["ready_to_push"] is True
    srv.shutdown()


# ── 명령줄 ───────────────────────────────────────────────────────────────────

from organum import hub_cli  # noqa: E402


def _cli(capsys, *argv) -> tuple[int, dict]:
    try:
        code = hub_cli.main(list(argv))
    except SystemExit as e:
        code = e.code
    text = capsys.readouterr().out.strip()
    if not text:
        return code, {}
    try:
        return code, json.loads(text)                    # 여러 줄로 찍는 명령(message)
    except ValueError:
        return code, json.loads(text.splitlines()[-1])


def test_cli_snapshot_restore_roundtrip(tmp_path, capsys):
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    snap = tmp_path / "state.snap"
    code, r = _cli(capsys, "snapshot", "--dir", str(h), "--outbox", f"{out}={DOOR}", "--out", str(snap))
    assert code == 0 and r["size"] == snap.stat().st_size and r["sha256"] == _sha(snap.read_bytes())
    assert _cli(capsys, "snapshot", "--dir", str(h), "--outbox", str(out), "--out", str(snap))[0] == 2
    h2, out2 = tmp_path / "b" / "hub", tmp_path / "b" / "out"
    code, r = _cli(capsys, "restore", "--snapshot", str(snap), "--dir", str(h2),
                   "--outbox", f"out-hub-ops={out2}")
    assert code == 0 and r["restored"] and _files(out2) == _files(out)
    # 빈 자리가 아니면 거부한다
    assert _cli(capsys, "restore", "--snapshot", str(snap), "--dir", str(h2),
                "--outbox", f"out-hub-ops={out2}")[0] == 2


def test_cli_checkpoint_exit_codes_reconcile_and_restore_from_slot(tmp_path, capsys):
    srv, url, mdir = _serve(tmp_path)
    (mdir / "settled").write_bytes(b"")
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    seed = tmp_path / "test.seed"
    seed.write_text(SEED.hex() + "\n", encoding="utf-8")
    tok = tmp_path / "client-token.txt"
    tok.write_text(TOKEN + "\n", encoding="utf-8")
    base = ["checkpoint", "--dir", str(h), "--outbox", f"{out}={DOOR}", "--key", str(seed),
            "--url", url, "--token-file", str(tok), "--wait", "0"]
    code, r = _cli(capsys, *base)
    assert code == 3 and r["generation"] == 1 and r["ready_to_push"] is False   # 받았으나 표지 전
    _mark_state(mdir, 1)
    code, r = _cli(capsys, *base)
    assert code == 0 and r["ready_to_push"] is True and r["unchanged"] is True
    door_url = f"{url}/v0/{DOOR}"
    code, r = _cli(capsys, "reconcile", "--local", str(out), "--url", door_url, "--token-file", str(tok))
    assert code == 0 and r["summary"] == {"only_local": ["001"]}
    _push(url, out, "001")
    (out / "001-body.md").write_bytes(b"changed locally")
    code, r = _cli(capsys, "reconcile", "--local", str(out), "--url", door_url, "--token-file", str(tok))
    assert code == 2 and r["outcomes"]["001"] == "mismatch"
    (out / "001-body.md").write_bytes(b"one")
    h2, out2 = tmp_path / "b" / "hub", tmp_path / "b" / "out"
    code, r = _cli(capsys, "restore", "--url", url, "--token-file", str(tok), "--pubkey", PUB,
                   "--dir", str(h2), "--outbox", f"out-hub-ops={out2}", "--wait", "0")
    assert code == 0 and r["generation"] == 1 and _files(out2) == _files(out)
    # 한도를 넘는 묶음을 미리 재 본다
    big = tmp_path / "big.bin"
    import os
    big.write_bytes(os.urandom(2_000_000))
    code, r = _cli(capsys, *base, "--dry-run", "--with-body", str(big))
    assert code == 3 and r["fits"] is False
    srv.shutdown()


def test_cli_message_refuses_an_oversized_body_before_admitting(tmp_path, capsys):
    srv, url, _ = _serve(tmp_path, marks=False, state_max_bytes=4000)
    h, out = _hub(tmp_path / "a")
    _add(h, out, b"one")
    _cp(h, out, url)
    seed = tmp_path / "test.seed"
    seed.write_text(SEED.hex() + "\n", encoding="utf-8")
    import os
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(8000))
    before = (h / "events.jsonl").read_bytes()
    args = ["message", "--dir", str(h), "--key", str(seed), "--signer", "lab:testonly", "--key-id", "k1",
            "--epoch", "1", "--to-lab", "lab:receiver", "--to-id", "R", "--to-epoch", "1"]
    code, _ = _cli(capsys, *args, "--body-file", str(big))
    assert code == 2 and (h / "events.jsonl").read_bytes() == before        # 원장에 넣지 않았다
    small = tmp_path / "small.md"
    small.write_bytes(b"fits")
    code, r = _cli(capsys, *args, "--body-file", str(small))
    assert code == 0 and (h / "events.jsonl").read_bytes() != before
    srv.shutdown()
