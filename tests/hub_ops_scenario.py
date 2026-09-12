"""hub CLI 골든 시나리오 — 구판(0.5.1, 5bd9bcc)과 신판(hub_ops 추출)을 **같은 입력**으로
돌려 stdout bytes·stderr bytes·exit code·quad bytes·원장 bytes를 대조하기 위한 헬퍼.

Orin 040 §1: "같은 새 함수를 두 번 호출해 맞는다고 하는 검사는 이 추출의 회귀 증거가
될 수 없다." 그래서 골든은 구판이 만든다 — `tests/fixtures/hub_ops_golden.json`은
5bd9bcc의 `git archive`를 PYTHONPATH로 두고 이 파일의 `run_scenario`를 서브프로세스로
돌려 채집했다(재생성: `python tests/hub_ops_scenario.py <workdir> > golden.json`).
시험(tests/test_hub_ops.py)은 신판으로 같은 시나리오를 in-process로 돌려 대조한다.

결정성 고정: seed 4개 고정 · machine_id 고정(init 뒤 hub.json 치환) · created_at 고정
(`_now_z`/`now_z` 패치) · **플랫폼 고정**(`sys.platform`은 봉투 provenance의 서명 입력이라
seed·시각·machine_id와 같은 부류로 고정 — Ray 101 F2) · 상대 경로(cwd=workdir) · 네트워크 0.
Windows 이식(F2): 서명된 bytes는 손대지 않는다. CLI **표시** 문자열의 경로 구분자와, 텍스트
모드로 쓰인 상태 파일(hub.json·events.jsonl)의 줄바꿈만 비교 전에 정규화한다 — 둘 다 서명
밖이다. quad 파일(바이너리)은 exact.
시나리오가 덮는 경로(040 §1 열거): 성공 message/export/verify · 같은 메시지 재시도 ·
잘못된 서명 · 본문 hash 불일치 · 미등록 키(admit 거부·TOFU 검증) · registry와 명시키
불일치 · 폐기된 키의 과거 봉투 감사 · export event-id 지정/오류.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

FROZEN_AT = "2026-09-12T00:00:00Z"
MACHINE_ID = "m-0123456789abcdef"
FROZEN_PLATFORM = "golden-platform"         # sys.platform 고정값(서명 입력)
TEXT_MODE_FILES = ("hub/hub.json", "hub/events.jsonl")   # 줄바꿈 정규화 대상(서명 밖)
SEEDS = {"a": bytes([7]) * 32, "b": bytes([9]) * 32,
         "c": bytes([11]) * 32, "b2": bytes([13]) * 32}


def _freeze_clock():
    import organum.hub_cli as cli
    cli._now_z = lambda: FROZEN_AT
    try:
        import organum.hub_ops as ho
        ho.now_z = lambda: FROZEN_AT
    except ImportError:                       # 구판에는 hub_ops가 없다
        pass
    return cli


def display_json(text: str, sep: str = os.sep) -> str:
    """JSON으로 직렬화된 stdout의 표시 정규화(Windows) — 경로 구분자는 JSON 안에서
    **이스케이프 쌍**(두 문자 `\\\\`)이므로 그 쌍만 '/'로 바꾼다(Ray 102 F2b: 단일 치환은
    쌍을 '//'로 만들었다). `\\n`·`\\"` 같은 다른 이스케이프는 건드리지 않는다."""
    return text.replace("\\\\", "/") if sep == "\\" else text


def display_text(text: str, sep: str = os.sep) -> str:
    """평문 stderr(오류 문구)의 표시 정규화 — 단일 역슬래시를 '/'로."""
    return text.replace("\\", "/") if sep == "\\" else text


def display(text: str, sep: str = os.sep) -> str:      # 경로 문자열(파일 목록) 용
    return display_text(text, sep)


def run_scenario(workdir: Path) -> dict:
    saved_platform = sys.platform
    sys.platform = FROZEN_PLATFORM
    try:
        return _run_scenario(workdir)
    finally:
        sys.platform = saved_platform


def _run_scenario(workdir: Path) -> dict:
    from organum import hub_envelope as he
    from organum import schnorr_pure as sp
    cli = _freeze_clock()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)
    pubs = {}
    for name, seed in SEEDS.items():
        p = workdir / f"{name}.seed"
        p.write_bytes(seed.hex().encode("ascii"))
        p.chmod(0o600)
        pubs[name] = sp.public_key(seed).hex()
    (workdir / "body1.md").write_bytes(b"hello board\n")
    (workdir / "body2.md").write_bytes(b"other body\n")

    steps: list[dict] = []

    def run(*argv) -> dict | None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        steps.append({"argv": list(argv), "rc": rc,
                      "stdout": display_json(out.getvalue()),
                      "stderr": display_text(err.getvalue())})
        try:
            return json.loads(out.getvalue())
        except ValueError:
            return None

    run("init", "--dir", "hub", "--source-domain", "lab:a/hub")
    cfg_p = workdir / "hub" / "hub.json"
    cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    cfg["machine_id"] = MACHINE_ID
    cfg_p.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")
    run("register-key", "--dir", "hub", "--signer", "lab:a", "--key-id", "k1",
        "--epoch", "1", "--pubkey", pubs["a"])
    run("register-key", "--dir", "hub", "--signer", "lab:b", "--key-id", "k1",
        "--epoch", "1", "--pubkey", pubs["b"])
    msg_a = ["message", "--dir", "hub", "--key", "a.seed", "--signer", "lab:a",
             "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:b", "--to-id", "Bee",
             "--to-epoch", "1", "--body-file", "body1.md"]
    run(*msg_a)
    run(*msg_a)                                          # 같은 메시지 재시도 → 수렴
    run("export", "--dir", "hub", "--out", "out", "--body", "body1.md")   # 001
    run("verify-envelope", "--envelope", "out/001-envelope.json",
        "--sig-file", "out/001-sig.txt", "--body", "body1.md", "--dir", "hub")
    run("verify-envelope", "--envelope", "out/001-envelope.json",
        "--sig-file", "out/001-sig.txt", "--body", "body2.md", "--dir", "hub")
    good_sig = (workdir / "out" / "001-sig.txt").read_text(encoding="utf-8").strip()
    bad_sig = ("f" if good_sig[0] != "f" else "0") + good_sig[1:]
    (workdir / "bad-sig.txt").write_text(bad_sig + "\n", encoding="utf-8")
    run("verify-envelope", "--envelope", "out/001-envelope.json",
        "--sig-file", "bad-sig.txt", "--dir", "hub")
    run("verify-envelope", "--envelope", "out/001-envelope.json",
        "--sig-file", "out/001-sig.txt", "--dir", "hub", "--pubkey", pubs["c"])
    # 미등록 서명자 C — admit 거부(원장 무변), 그리고 TOFU 검증
    run("message", "--dir", "hub", "--key", "c.seed", "--signer", "lab:c",
        "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:a", "--to-id", "Ann",
        "--to-epoch", "1", "--body-file", "body1.md")
    body1 = (workdir / "body1.md").read_bytes()
    digest = hashlib.sha256(body1).hexdigest()
    env_c = cli._build_envelope(
        cfg, "message.posted",
        {"target": {"lab_id": "lab:a", "to_id": "Ann", "to_epoch": 1},
         "body_locator": "file://body1.md", "body_sha256": digest,
         "body_media_type": "text/markdown"},
        signer="lab:c", key_id="k1", epoch=1,
        subject={"type": "message", "id": "message:" + digest[:24]})
    raw_c = he.canonical_bytes(env_c)
    sig_c = sp.sign(hashlib.sha256(raw_c).digest(), SEEDS["c"]).hex()
    (workdir / "c-env.json").write_bytes(raw_c)
    (workdir / "c-sig.txt").write_text(sig_c + "\n", encoding="utf-8")
    run("verify-envelope", "--envelope", "c-env.json", "--sig-file", "c-sig.txt",
        "--dir", "hub")
    run("verify-envelope", "--envelope", "c-env.json", "--sig-file", "c-sig.txt",
        "--pubkey", pubs["c"])
    run("verify-envelope", "--envelope", "c-env.json", "--sig-file", "c-sig.txt",
        "--dir", "hub", "--pubkey", pubs["c"], "--body", "body1.md")
    run("verify-envelope", "--envelope", "c-env.json", "--sig-file", "c-sig.txt")
    # B: 발신 → export → 키 회전·폐기 → 과거 봉투 감사
    r_b = run("message", "--dir", "hub", "--key", "b.seed", "--signer", "lab:b",
              "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:a", "--to-id", "Ann",
              "--to-epoch", "1", "--body-file", "body1.md")
    run("export", "--dir", "hub", "--out", "out", "--body", "body1.md")   # 002
    run("rotate-key", "--dir", "hub", "--key", "b.seed", "--signer", "lab:b",
        "--key-id", "k1", "--epoch", "1", "--new-key-id", "k2", "--new-epoch", "2",
        "--new-pubkey", pubs["b2"])
    run("revoke-key", "--dir", "hub", "--key", "b2.seed", "--signer", "lab:b",
        "--key-id", "k2", "--epoch", "2", "--revoke-key-id", "k1",
        "--revoke-epoch", "1", "--reason", "scenario")
    run("verify-envelope", "--envelope", "out/002-envelope.json",
        "--sig-file", "out/002-sig.txt", "--body", "body1.md", "--dir", "hub")
    run("export", "--dir", "hub", "--out", "out", "--event-id",
        (r_b or {}).get("event_id", "missing"))                           # 003
    run("export", "--dir", "hub", "--out", "out", "--event-id", "deadbeef")
    run("export", "--dir", "hub", "--out", "out", "--body", "nope.md")
    run("list", "--dir", "hub")
    run("verify-envelope", "--envelope", "out/001-envelope.json",
        "--sig", "ab", "--sig-file", "out/001-sig.txt")
    run("message", "--dir", "hub", "--key", "a.seed", "--signer", "lab:a",
        "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:b", "--to-id", "Bee",
        "--to-epoch", "1", "--body-file", "nope.md")
    run("verify-envelope", "--envelope", "out/001-envelope.json",
        "--sig-file", "out/001-sig.txt", "--dir", "nohub")

    files = {}
    for rel in list(TEXT_MODE_FILES) + sorted(
            display(str(p.relative_to(workdir))) for p in (workdir / "out").iterdir()):
        data = (workdir / rel).read_bytes()
        if rel in TEXT_MODE_FILES:
            data = data.replace(b"\r\n", b"\n")       # 텍스트 모드 줄바꿈만(서명 밖)
        files[rel] = hashlib.sha256(data).hexdigest()
    return {"frozen_at": FROZEN_AT, "machine_id": MACHINE_ID,
            "platform": FROZEN_PLATFORM, "steps": steps, "files": files}


if __name__ == "__main__":
    result = run_scenario(Path(sys.argv[1]))
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
