"""hub 상태의 저장·복원·대조·체크포인트 (0.8.0).

설계: docs/hub-state-snapshot-restore-reconcile-v0-design.md (v0.12). 무엇을 하는가:

- **저장(snapshot)**: 원장과 발신함을 같은 시점으로 묶어 파일 하나로 낸다. 키는 다루지 않는다.
- **복원(restore)**: 그 묶음에서 빈 자리에 되살린다. 재생으로 검증하고, 본문은 쓰기 전에 지문을 본다.
- **대조(reconcile)**: 로컬 디렉터리와 드롭의 문을 번호와 바이트의 지문으로 견준다. 읽기 전용이다.
- **체크포인트(checkpoint)**: 묶음을 드롭의 상태 칸에 올리고, 바깥에 옮겨진 것(표지)을 본 뒤에 끝난다.
  편지는 그 다음에 올린다 — "올리기 전에 저장한다".

묶음의 모양(zlib로 압축한 바이트열 하나):

    organum-hub/snapshot/v0\\n
    <목록의 sha256, hex 64자>\\n
    <목록: JSON 한 줄>\\n
    <조각의 바이트를 목록에 적힌 순서대로 이어 붙인 것>

조각의 이름은 `hub.json`, `events.jsonl`, `body/<발신함 이름>/<번호>` 셋뿐이다. 경로가 아니라 이름표다.

출처: 저장·복원·대조의 뼈대는 Jdot HQ의 시제품(`ledger_helper.py`, 2026-10-04·05)에서 왔다. 그 실험이
드러낸 것(불완전한 quad, 넣고 내보내지 않은 편지, 쓰기 전의 지문 확인)을 그대로 지킨다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from . import __version__
from . import hub_drop as hd
from . import hub_envelope as he
from . import hub_ops as ho
from . import schnorr_pure as sp

MAGIC = b"organum-hub/snapshot/v0\n"
EXPANDED_MAX_BYTES = 16 * 1_048_576     # 푼 크기의 상한(잠정) — 부푸는 묶음을 막는다
RECORD_FILE = "state-slot.json"         # 올리는 쪽의 작은 기록(원장 밖). 받아들여진 세대와 보낸 번호
RECORD_KEEP = 32                        # 기록에 남기는 받아들여진 세대의 수
MARK_WAIT_SECONDS = 60                  # 표지를 기다리는 기본 시간. 넘으면 편지를 올리지 않고 끝낸다
RESTORE_WAIT_SECONDS = 600              # 복원이 자리를 잡았다는 신호를 기다리는 시간 — 8분보다 길게
MARK_POLL_SECONDS = 5.0                 # 표지를 묻는 간격. 미러 패스가 몇 초마다 돈다
RESTORE_POLL_SECONDS = 15.0             # 자리를 잡았는지 묻는 간격. 분 단위의 일이다(LxM 134 §5)

_OUTBOX_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_DOOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}/from-[a-z0-9][a-z0-9-]{0,63}\Z")
_N_RE = re.compile(r"^[0-9]{3,6}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")
_BODY_PART_RE = re.compile(r"^body/([A-Za-z0-9_-]{1,64})/([0-9]{3,6})\Z")
_BODY_FILE_RE = re.compile(r"^body\.[a-z0-9]{1,8}\Z")


class StateError(ValueError):
    """묶음이나 상태가 계약에 맞지 않는다. 문구는 CLI가 그대로 찍는다."""


class SnapshotRefused(StateError):
    """저장을 거부하는 상태 — 불완전한 quad, 넣고 내보내지 않은 편지, bbs 발신 기록."""


class CheckpointStopped(StateError):
    """체크포인트가 멈췄다. `reason`이 까닭이고 편지를 올려서는 안 된다."""

    def __init__(self, reason: str, message: str, **detail):
        super().__init__(message)
        self.reason = reason
        self.detail = detail


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _strict_json(raw: bytes | str):
    """중복된 키를 거부하는 JSON 읽기 — 같은 키가 두 번이면 읽는 쪽마다 다른 값을 본다."""
    def pairs(ps):
        d = {}
        for k, v in ps:
            if k in d:
                raise StateError(f"JSON에 같은 키가 두 번 있다: {k}")
            d[k] = v
        return d
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeDecodeError) as e:
        if isinstance(e, StateError):
            raise
        raise StateError(f"JSON이 아니다: {e}") from None


# ── 묶음 ─────────────────────────────────────────────────────────────────────

def pack(manifest: dict, parts: list[tuple[str, bytes]]) -> bytes:
    """목록과 조각을 묶음으로. 조각을 base64로 바꾸지 않는다 — 글자로 바꾼 뒤에 압축하면 덜 줄어든다."""
    m = dict(manifest)
    m["parts"] = [{"name": name, "size": len(b), "sha256": _sha(b)} for name, b in parts]
    line = _canonical(m)
    raw = MAGIC + _sha(line).encode("ascii") + b"\n" + line + b"\n" + b"".join(b for _, b in parts)
    return zlib.compress(raw, 9)


def _inflate(blob: bytes, limit: int) -> bytes:
    """완결된 zlib 스트림 하나만 받는다(Jdot HQ 실험). 잘린 것, 뒤에 바이트가 붙은 것, 스트림이 둘 이상인
    것은 서명이 맞아도 거부한다. 풀다가 상한을 넘으면 거기서 그친다."""
    d = zlib.decompressobj()
    try:
        out = d.decompress(blob, limit + 1)
    except zlib.error as e:
        raise StateError(f"묶음을 풀 수 없다: {e}") from None
    if len(out) > limit or d.unconsumed_tail:
        raise StateError(f"묶음이 푼 크기의 상한({limit} 바이트)을 넘는다")
    if not d.eof:
        raise StateError("묶음이 잘렸다 — 완결된 zlib 스트림이 아니다")
    if d.unused_data:
        raise StateError("묶음 뒤에 바이트가 붙어 있다 — 스트림은 하나여야 한다")
    return out


def unpack(blob: bytes, *, max_bytes: int | None = None,
           expanded_max: int = EXPANDED_MAX_BYTES) -> tuple[dict, dict[str, bytes]]:
    """묶음 → (목록, 조각). 첫 줄, 목록의 지문, 조각마다의 크기와 지문을 본다. 모르는 조각 이름과
    크기의 합이 맞지 않는 묶음을 거부한다. **이 확인은 옮기다 깨진 것을 잡을 뿐, 누가 만들었는지나
    최신인지를 증명하지 않는다** — 그것은 서명과 상태 칸의 조건이 한다."""
    if max_bytes is not None and len(blob) > max_bytes:
        raise StateError(f"묶음이 한도({max_bytes} 바이트)를 넘는다")
    raw = _inflate(blob, expanded_max)
    if not raw.startswith(MAGIC):
        raise StateError("묶음의 첫 줄이 다르다 — organum-hub/snapshot/v0가 아니다")
    rest = raw[len(MAGIC):]
    sha_line, sep, rest = rest.partition(b"\n")
    line, sep2, payload = rest.partition(b"\n")
    if not (sep and sep2):
        raise StateError("묶음의 머리가 모자란다")
    if _sha(line) != sha_line.decode("ascii", "replace"):
        raise StateError("목록의 지문이 맞지 않는다")
    manifest = _strict_json(line)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("parts"), list):
        raise StateError("목록의 모양이 다르다")
    parts: dict[str, bytes] = {}
    offset = 0
    for p in manifest["parts"]:
        name, size, sha = (p.get("name"), p.get("size"), p.get("sha256")) if isinstance(p, dict) \
            else (None, None, None)
        if not (isinstance(name, str) and (name in ("hub.json", "events.jsonl")
                                           or _BODY_PART_RE.match(name))):
            raise StateError(f"모르는 조각 이름: {name!r}")
        if name in parts:
            raise StateError(f"조각이 두 번 있다: {name}")
        if not (type(size) is int and size >= 0 and isinstance(sha, str) and _SHA_RE.match(sha)):
            raise StateError(f"조각의 크기나 지문이 모양에 맞지 않는다: {name}")
        chunk = payload[offset:offset + size]
        offset += size
        if len(chunk) != size or _sha(chunk) != sha:
            raise StateError(f"조각의 바이트가 목록과 다르다: {name}")
        parts[name] = chunk
    if offset != len(payload):
        raise StateError("조각 크기의 합이 남은 길이와 다르다")
    if "hub.json" not in parts or "events.jsonl" not in parts:
        raise StateError("묶음에 원장이 없다")
    return manifest, parts


# ── 원장과 발신함 읽기 ────────────────────────────────────────────────────────

def _ledger_rows(events: bytes) -> list[dict]:
    rows = []
    for i, line in enumerate(events.decode("utf-8").splitlines(), 1):
        rec = _strict_json(line)
        if rec.get("transport") != "direct":
            # v0는 direct 경로의 줄만 다룬다(설계 §9). wire 줄이 있으면 조용히 넘기지 않는다.
            raise SnapshotRefused(f"원장 {i}번째 줄이 wire 경유다 — v0는 direct 줄만 다룬다")
        rows.append(rec)
    return rows


def _rows_by_event(rows: list[dict]) -> dict[str, dict]:
    return {he.event_id_of(r["raw"].encode("utf-8")): r for r in rows}


def _scan_outbox(name: str, out: Path, by_event: dict[str, dict]) -> tuple[list[dict], dict[str, tuple[str, bytes]]]:
    """발신함 → (번호 대응, 본문). 불완전한 quad와 bbs 발신 기록이 있으면 거부한다.

    서명 파일만 남은 잔재도 다음 번호를 민다(`next_quad_number`). 그것을 빼고 저장하면 되살린 뒤
    번호가 되돌아간다 — 그래서 뺄 수 없고, 저장을 거부한다(Jdot HQ 실험)."""
    if not out.is_dir():
        raise SnapshotRefused(f"발신함이 없다: {out}")
    by_n: dict[str, list[Path]] = {}
    for f in out.iterdir():
        if f.name.endswith(".outbox.json"):
            raise SnapshotRefused(
                f"{name}: bbs의 발신 기록({f.name})이 있다 — 절대 경로가 들어 있어 v0는 받지 않는다")
        n = f.name.split("-", 1)[0]
        if f.name != n and _N_RE.match(n):
            by_n.setdefault(n, []).append(f)
    index, bodies = [], {}
    for n in sorted(by_n, key=int):
        env_p, sig_p = out / f"{n}-envelope.json", out / f"{n}-sig.txt"
        if not (env_p.is_file() and sig_p.is_file()):
            raise SnapshotRefused(
                f"{name}: {n}번이 불완전하다(봉투나 서명이 없다) — 중단된 내보내기의 잔재다. "
                "치우거나 마저 내보낸 뒤에 저장한다")
        env_b = env_p.read_bytes()
        eid = he.event_id_of(env_b)
        row = by_event.get(eid)
        if row is None or row["raw"].encode("utf-8") != env_b \
                or sig_p.read_bytes() != (row["sig"] + "\n").encode("utf-8"):
            raise SnapshotRefused(f"{name}: {n}번의 봉투나 서명이 원장과 다르다")
        digest = _strict_json(env_b).get("payload", {}).get("body_sha256")
        body_files = sorted(out.glob(f"{n}-body.*"))
        if len(body_files) > 1:
            raise SnapshotRefused(f"{name}: {n}번에 본문 파일이 둘 이상이다")
        body_name = None
        if body_files:
            body_name = body_files[0].name[len(n) + 1:]
            body_b = body_files[0].read_bytes()
            if not _BODY_FILE_RE.match(body_name) or _sha(body_b) != digest:
                raise SnapshotRefused(f"{name}: {n}번의 본문이 봉투의 지문과 다르다")
            bodies[n] = (body_name, body_b)
        index.append({"n": n, "event_id": eid, "body_name": body_name, "body_sha256": digest})
    return index, bodies


def _own_lab(cfg: dict) -> str | None:
    """이 hub를 운영하는 연구소. `source_domain`의 첫 조각에서 정한다 — 원장이 도입 권한을 정하는 것과
    같은 규칙이다(`HubIndex._introducer_authority`). `hub.json`의 `keys`로 가리지 않는다. 거기에는
    자기 키만이 아니라 등록해 둔 상대 연구소의 검증 키도 있다. 그것으로 가리면 받은 편지가 자기가
    넣고 내보내지 않은 편지로 보인다(Orin 053 A)."""
    head = str(cfg.get("source_domain") or "").split("/", 1)[0]
    if he._LAB_ID.fullmatch(head):
        return head
    cand = f"lab:{head}"
    if he._LAB_ID.fullmatch(cand) and any(k.get("signer_id") == cand for k in cfg.get("keys") or []):
        return cand
    return None


# ── 저장 ─────────────────────────────────────────────────────────────────────

def snapshot(hub_dir, outboxes: dict[str, Path], *, doors: dict[str, str] | None = None,
             omit_bodies: dict[str, set[str]] | None = None, generation: int | None = None,
             prev_sha256: str | None = None, supersedes=(), allow_unexported: bool = False,
             created_at: str | None = None, _locked: bool = False) -> bytes:
    """원장과 발신함을 같은 시점으로 묶는다. 쓰기 잠금 아래에서 뜬다.

    `omit_bodies`: 싣지 않을 본문의 번호(발신함 이름마다). 그 본문은 드롭에서 되받는다는 뜻이므로,
    드롭에 같은 바이트로 있는 것을 확인한 번호만 넣는다. `checkpoint`가 표지를 보고 채운다.
    묶음에 비밀을 넣지 않는다 — 정해진 이름의 파일(`hub.json`, `events.jsonl`, quad)만 읽는다."""
    hub = Path(hub_dir)
    doors = doors or {}
    for name in outboxes:
        if not _OUTBOX_NAME_RE.match(name):
            raise StateError(f"발신함 이름은 [A-Za-z0-9_-]: {name!r}")
        if name in doors and not _DOOR_RE.match(doors[name]):
            raise StateError(f"문은 <channel>/<from-x>: {doors[name]!r}")

    def build() -> bytes:
        if not (hub / "hub.json").is_file():
            raise StateError(f"hub 상태가 없다: {hub}")
        hub_json = (hub / "hub.json").read_bytes()
        events = (hub / "events.jsonl").read_bytes() if (hub / "events.jsonl").is_file() else b""
        rows = _ledger_rows(events)
        by_event = _rows_by_event(rows)
        entries, parts, exported = [], [("hub.json", hub_json), ("events.jsonl", events)], set()
        for name, out in outboxes.items():
            index, bodies = _scan_outbox(name, Path(out), by_event)
            exported.update(i["event_id"] for i in index)
            skip = (omit_bodies or {}).get(name, set())
            for n in sorted(bodies, key=int):
                if n not in skip:
                    parts.append((f"body/{name}/{n}", bodies[n][1]))
            entries.append({"name": name, "door": doors.get(name), "index": index,
                            "next_n": ho.next_quad_number(Path(out))})
        # 넣고 내보내지 않은 편지: 본문이 원장에도 발신함에도 없다. 되살릴 수 없으므로 저장을 거부한다.
        own = _own_lab(_strict_json(hub_json))
        if own is None and not allow_unexported:
            raise SnapshotRefused(
                "이 hub를 운영하는 연구소를 source_domain에서 정할 수 없다 — 어느 편지가 자기 것인지 "
                "가릴 수 없어 넣고 내보내지 않은 편지를 찾지 못한다. 저장하지 않는다")
        unexported = []
        for eid, row in by_event.items():
            env = _strict_json(row["raw"])
            if (env.get("event_kind") == "message.posted" and own is not None
                    and env.get("signer", {}).get("id") == own and eid not in exported):
                unexported.append(eid)
        if unexported and not allow_unexported:
            raise SnapshotRefused(
                f"자기 연구소({own})가 서명한 편지 {len(unexported)}통이 어느 발신함에도 없다 — 넣고 내보내지 않았거나 "
                "다른 발신함에 있다. 그 발신함도 함께 주거나, 본문을 잃는 것을 받아들이면 "
                "--allow-unexported")
        manifest = {"created_at": created_at or ho.now_z(), "organum_version": __version__,
                    "hub": {"events": len(rows)}, "outboxes": entries,
                    "generation": generation, "prev_sha256": prev_sha256,
                    "supersedes": sorted(supersedes), "unexported": sorted(unexported)}
        return pack(manifest, parts)

    if _locked:
        return build()
    with ho.hub_write_lock(hub):
        return build()


# ── 복원 ─────────────────────────────────────────────────────────────────────

def _empty(p: Path) -> bool:
    return not p.exists() or (p.is_dir() and not any(p.iterdir()))


def restore(blob: bytes, hub_dir, outbox_dirs: dict[str, Path], *, max_bytes: int | None = None) -> dict:
    """묶음에서 **빈 자리에** 되살린다. 있는 것을 덮어쓰지 않는다.

    임시 자리에 원장을 쓰고 재생해서 검증한 뒤에 제자리로 옮긴다. 발신함의 봉투와 서명은 원장의 줄에서
    다시 만든다. 본문은 **쓰기 전에** 봉투의 지문과 대조한다. 묶음에 없는 본문은 "드롭에서 받아야 함"으로
    돌려준다 — `pull`로 채우지 않는다(`pull`은 서버가 준 본문을 대조 없이 쓴다)."""
    manifest, parts = unpack(blob, max_bytes=max_bytes)
    hub = Path(hub_dir)
    entries = manifest.get("outboxes")
    if not isinstance(entries, list):
        raise StateError("목록에 발신함이 없다")
    names = [e.get("name") for e in entries if isinstance(e, dict)]
    if len(names) != len(entries) or len(set(names)) != len(names) \
            or not all(isinstance(n, str) and _OUTBOX_NAME_RE.match(n) for n in names):
        raise StateError("목록의 발신함 이름이 모양에 맞지 않거나 겹친다")
    missing_dirs = [n for n in names if n not in outbox_dirs]
    if missing_dirs:
        raise StateError(f"되살릴 자리를 주지 않은 발신함: {', '.join(missing_dirs)}")
    for target in [hub] + [Path(outbox_dirs[n]) for n in names]:
        if not _empty(target):
            raise StateError(f"대상이 비어 있지 않다 — 복원은 빈 자리에만 한다: {target}")
    rows = _ledger_rows(parts["events.jsonl"])
    if manifest.get("hub", {}).get("events") != len(rows):
        raise StateError("원장의 줄 수가 목록과 다르다")
    by_event = _rows_by_event(rows)
    hub.parent.mkdir(parents=True, exist_ok=True)
    need_fetch: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix=".organum-restore-", dir=hub.parent) as td:
        stage = Path(td)
        (stage / "hub").mkdir()
        (stage / "hub" / "hub.json").write_bytes(parts["hub.json"])
        (stage / "hub" / "events.jsonl").write_bytes(parts["events.jsonl"])
        try:
            ho.load_hub(stage / "hub")                      # 재생이 끝까지 가지 않으면 복원 실패다
        except Exception as e:                              # noqa: BLE001 — 어떤 재생 실패든 복원 실패
            raise StateError(f"복원한 원장이 재생되지 않는다: {e}") from None
        staged = {}
        for e in entries:
            name, out = e["name"], stage / "out" / e["name"]
            out.mkdir(parents=True)
            seen: set[str] = set()
            for i in e.get("index") or []:
                n, eid = i.get("n"), i.get("event_id")
                if not (isinstance(n, str) and _N_RE.match(n)) or n in seen:
                    raise StateError(f"{name}: 번호가 모양에 맞지 않거나 겹친다: {n!r}")
                seen.add(n)
                row = by_event.get(eid)
                if row is None:
                    raise StateError(f"{name}: {n}번의 이벤트가 원장에 없다")
                env_b = row["raw"].encode("utf-8")
                digest = _strict_json(env_b).get("payload", {}).get("body_sha256")
                if digest != i.get("body_sha256"):
                    raise StateError(f"{name}: {n}번의 본문 지문이 봉투와 다르다")
                body_name = i.get("body_name")
                if body_name is not None and not (isinstance(body_name, str)
                                                  and _BODY_FILE_RE.match(body_name)):
                    raise StateError(f"{name}: {n}번의 본문 이름이 모양에 맞지 않는다")
                (out / f"{n}-sig.txt").write_bytes((row["sig"] + "\n").encode("utf-8"))
                body = parts.get(f"body/{name}/{n}")
                if body_name is not None:
                    if body is None:
                        need_fetch.setdefault(name, []).append(n)
                    elif _sha(body) != digest:               # 쓰기 전에 본다
                        raise StateError(f"{name}: {n}번의 본문이 봉투의 지문과 다르다")
                    else:
                        (out / f"{n}-{body_name}").write_bytes(body)
                elif body is not None:
                    raise StateError(f"{name}: {n}번에는 본문이 없어야 한다")
                (out / f"{n}-envelope.json").write_bytes(env_b)      # 봉투가 완결 표지 — 마지막
            if ho.next_quad_number(out) != e.get("next_n"):
                raise StateError(f"{name}: 되살린 다음 번호가 묶음의 것({e.get('next_n')})과 다르다")
            staged[name] = out
        stray = [k for k in parts if k.startswith("body/") and k.split("/")[1] not in staged]
        if stray:
            raise StateError(f"목록에 없는 발신함의 본문이 있다: {stray[0]}")
        if hub.exists():
            hub.rmdir()
        shutil.move(str(stage / "hub"), str(hub))
        for name, out in staged.items():
            target = Path(outbox_dirs[name])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.rmdir()
            shutil.move(str(out), str(target))
    return {"restored": True, "events": len(rows),
            "outboxes": {e["name"]: {"quads": len(e.get("index") or []), "next_n": e.get("next_n"),
                                     "door": e.get("door"),
                                     "bodies_to_fetch": need_fetch.get(e["name"], [])}
                         for e in entries},
            "generation": manifest.get("generation"), "unexported": manifest.get("unexported") or []}


# ── 드롭과 말하기 ────────────────────────────────────────────────────────────

def _http(url: str, token: str, data: dict | None = None,
          timeout: int = hd.CLIENT_TIMEOUT_SECONDS) -> tuple[int, dict]:
    """상태 코드와 본문을 함께 돌려준다 — 상태 칸은 404와 409의 본문에 지금 세대를 싣는다."""
    raw = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=raw, method="POST" if raw is not None else "GET",
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if raw else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:                                    # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        if e.code == 429:
            try:
                body["retry_after"] = min(max(int(e.headers.get("Retry-After", "")), 1), 120)
            except ValueError:
                body["retry_after"] = 5
        return e.code, body


def _pause(st: int, body: dict, default: float) -> float:
    """다음에 묻기까지 쉴 시간. 빈도 한도(429)에 걸렸으면 서버가 말한 만큼은 쉰다."""
    if st != 429:
        return default
    return max(default, float(body.get("retry_after") or 0))


def _get_patient(url: str, token: str, timeout: int, sleep, clock, deadline: float) -> tuple[int, dict]:
    """읽기 한 번. 429이면 서버가 말한 만큼 쉬고 다시 묻는다 — 시한까지. 빈도 한도는 그만둘 까닭이 아니다."""
    while True:
        st, body = _http(url, token, timeout=timeout)
        if st != 429 or clock() >= deadline:
            return st, body
        sleep(_pause(st, body, 1.0))


def fetch_index(door_url: str, token: str, timeout: int = hd.CLIENT_TIMEOUT_SECONDS) -> list[dict] | None:
    """문 색인을 처음부터 끝까지. 색인이 없는 서버(0.7.0 이하)면 None."""
    out, since = [], "000"
    while True:
        st, body = _http(f"{door_url}?index=1&since={since}", token, timeout=timeout)
        if st != 200:
            raise StateError(f"문 색인을 받지 못했다: HTTP {st} {body.get('error', '')}")
        if "index" not in body:
            return None
        out.extend(body["index"])
        if not body.get("more"):
            return out
        if not body["index"]:
            raise StateError("문 색인이 나아가지 않는다(더 있다면서 빈 쪽을 줬다)")
        since = body["index"][-1]["n"]


def _local_fingerprints(local: Path) -> dict[str, dict]:
    """로컬 디렉터리의 번호마다 저장된 바이트의 지문 — 서버의 색인과 같은 모양."""
    out: dict[str, dict] = {}
    if not local.is_dir():
        return out
    numbers = {f.name.split("-", 1)[0] for f in local.iterdir()
               if f.name != f.name.split("-", 1)[0] and _N_RE.match(f.name.split("-", 1)[0])}
    for n in numbers:
        env_p, sig_p = local / f"{n}-envelope.json", local / f"{n}-sig.txt"
        bodies = sorted(local.glob(f"{n}-body.*"))
        out[n] = {"envelope_sha256": _sha(env_p.read_bytes()) if env_p.is_file() else None,
                  "sig_sha256": _sha(sig_p.read_bytes()) if sig_p.is_file() else None,
                  "body_name": bodies[0].name[len(n) + 1:] if bodies else None,
                  "body_sha256": _sha(bodies[0].read_bytes()) if bodies else None}
    return out


def _remote_fingerprints(door_url: str, token: str, timeout: int) -> dict[str, dict]:
    index = fetch_index(door_url, token, timeout=timeout)
    if index is not None:
        return {e["n"]: e for e in index}
    # 색인이 없는 서버: 문 전체를 넘기며 받은 바이트의 지문을 직접 낸다.
    out, since = {}, "000"
    while True:
        page = hd.fetch_page(door_url, token, since, timeout=timeout)
        for q in page["quads"]:
            body = base64.b64decode(q["body_b64"]) if q.get("body_name") else None
            out[q["n"]] = {"envelope_sha256": _sha(base64.b64decode(q["envelope_b64"])),
                           "sig_sha256": _sha((q["sig"] + "\n").encode("utf-8")),
                           "body_name": q.get("body_name"),
                           "body_sha256": _sha(body) if body is not None else None}
            since = q["n"]
        if not page.get("more"):
            return out
        if not page["quads"]:
            raise StateError("문이 나아가지 않는다(더 있다면서 빈 쪽을 줬다)")


# ── 대조 ─────────────────────────────────────────────────────────────────────

_RECONCILE_EXIT = {"in_sync": 0, "only_local": 0, "only_remote": 1, "incomplete_local": 1, "mismatch": 2}


def reconcile(local_dir, door_url: str, token: str, *,
              timeout: int = hd.CLIENT_TIMEOUT_SECONDS) -> dict:
    """로컬 디렉터리와 드롭의 그 문을 **처음부터 끝까지** 견준다. 읽기 전용이고 고치지 않는다.

    견주는 것은 바이트의 지문이다. 봉투를 읽어 다시 쓰지 않는다. 서버가 "더 없음"이라고 거짓으로 답하면
    알아챌 수 없다 — 믿을 수 있는 저장소가 전제다."""
    local = _local_fingerprints(Path(local_dir))
    remote = _remote_fingerprints(door_url, token, timeout)
    outcomes: dict[str, str] = {}
    for n in sorted(set(local) | set(remote), key=int):
        loc, rem = local.get(n), remote.get(n)
        if loc is None:
            outcomes[n] = "only_remote"
        elif loc["envelope_sha256"] is None:
            outcomes[n] = "incomplete_local"                 # 봉투가 없으면 완결이 아니다
        elif rem is None:
            outcomes[n] = "incomplete_local" if loc["sig_sha256"] is None else "only_local"
        elif loc["envelope_sha256"] != rem["envelope_sha256"]:
            outcomes[n] = "mismatch"
        elif loc["sig_sha256"] is None or (rem.get("body_name") and loc["body_sha256"] is None):
            outcomes[n] = "incomplete_local"
        elif (loc["sig_sha256"], loc["body_name"], loc["body_sha256"]) != \
                (rem["sig_sha256"], rem.get("body_name"), rem.get("body_sha256")):
            outcomes[n] = "mismatch"
        else:
            outcomes[n] = "in_sync"
    exit_code = max((_RECONCILE_EXIT[v] for v in outcomes.values()), default=0)
    summary: dict[str, list[str]] = {}
    for n, v in outcomes.items():
        summary.setdefault(v, []).append(n)
    return {"exit": exit_code, "outcomes": outcomes, "summary": summary,
            "blocked": exit_code != 0}


# ── 올리는 쪽의 기록 ─────────────────────────────────────────────────────────

def _record_path(hub_dir) -> Path:
    return Path(hub_dir) / RECORD_FILE


def load_record(hub_dir) -> dict:
    p = _record_path(hub_dir)
    if not p.is_file():
        return {"acked": [], "pending": None, "floor": 0, "max_bytes": None, "outboxes": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_record(hub_dir, rec: dict) -> None:
    rec["acked"] = rec.get("acked", [])[-RECORD_KEEP:]
    p = _record_path(hub_dir)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)


def _acked(rec: dict, generation: int, sha: str) -> dict | None:
    return next((a for a in rec.get("acked", []) if a["generation"] == generation and a["sha256"] == sha),
                None)


_SUMMARY_KEYS = ("events", "events_sha256", "next_n", "outbox_map")


def _envelope_pairs(out: Path, upto: str | None = None) -> list[list]:
    """발신함의 [번호, 봉투 바이트의 지문]을 번호 순으로. `upto`를 주면 그 번호까지만."""
    pairs = []
    if out.is_dir():
        for f in out.glob("*-envelope.json"):
            n = f.name.split("-", 1)[0]
            if _N_RE.match(n) and (upto is None or int(n) <= int(upto)):
                pairs.append([n, _sha(f.read_bytes())])
    pairs.sort(key=lambda t: int(t[0]))
    return pairs


def _outbox_map(out: Path, door: str | None, upto: str | None = None) -> dict:
    """문과 번호 → 봉투의 대응을 작게 적은 것. 받아들여진 세대마다 기록에 남긴다(Orin 053 B)."""
    pairs = _envelope_pairs(out, upto)
    return {"door": door, "count": len(pairs), "max_n": pairs[-1][0] if pairs else None,
            "sha256": _sha(_canonical(pairs))}


def _ledger_summary(hub: Path, outboxes: dict[str, Path], doors: dict[str, str] | None = None) -> dict:
    """묶음 하나가 아는 것의 요약: 원장의 줄 수와 지문, 발신함마다 다음 번호, 문과 번호 대응의 지문."""
    events = (hub / "events.jsonl").read_bytes() if (hub / "events.jsonl").is_file() else b""
    doors = doors or {}
    return {"events": events.count(b"\n"), "events_sha256": _sha(events),
            "next_n": {name: ho.next_quad_number(Path(out)) for name, out in outboxes.items()},
            "outbox_map": {name: _outbox_map(Path(out), doors.get(name))
                           for name, out in outboxes.items()}}


def _check_extension(hub: Path, outboxes: dict[str, Path], doors: dict[str, str], last: dict) -> None:
    """올리는 묶음은 올라앉을 세대의 연장이어야 한다(불변식 10). 원장이 지난번의 줄들로 시작하고,
    다음 번호가 줄지 않았고, **문과 번호 → 편지의 대응이 지난번 그대로**인지 본다. 묶음에서 편지를 빼거나
    번호를 바꾸는 것으로는 번호를 받은 편지를 취소하지 못한다. 드롭의 그 번호는 이미 한 편지의 것이다."""
    events = (hub / "events.jsonl").read_bytes() if (hub / "events.jsonl").is_file() else b""
    lines = events.split(b"\n")
    head = b"\n".join(lines[:last["events"]]) + (b"\n" if last["events"] else b"")
    if events.count(b"\n") < last["events"] or _sha(head) != last["events_sha256"]:
        raise CheckpointStopped("not_an_extension",
                                "원장이 지난번에 받아들여진 세대의 줄들로 시작하지 않는다 — 올리지 않는다")
    for name, nxt in (last.get("next_n") or {}).items():
        if name in outboxes and int(ho.next_quad_number(Path(outboxes[name]))) < int(nxt):
            raise CheckpointStopped("not_an_extension",
                                    f"{name}: 다음 번호가 줄었다 — 올리지 않는다")
    for name, old in (last.get("outbox_map") or {}).items():
        if name not in outboxes:
            raise CheckpointStopped("not_an_extension",
                                    f"{name}: 지난번에 실은 발신함이 이번에는 없다 — 올리지 않는다. "
                                    "발신함의 이름은 디렉터리의 이름이다(되살린 디렉터리의 이름을 바꾸지 않는다)")
        if doors.get(name) != old.get("door"):
            raise CheckpointStopped("not_an_extension",
                                    f"{name}: 문이 지난번({old.get('door')})과 다르다 — 올리지 않는다")
        if old.get("max_n") is None:
            continue
        now = _outbox_map(Path(outboxes[name]), old.get("door"), upto=old["max_n"])
        if (now["count"], now["sha256"]) != (old["count"], old["sha256"]):
            raise CheckpointStopped(
                "not_an_extension",
                f"{name}: {old['max_n']}번까지의 번호가 가리키는 편지가 지난번과 다르다 — 올리지 않는다")


def _extends(old_manifest: dict, old_parts: dict, new_manifest: dict, new_parts: dict) -> bool:
    """`new`가 `old`를 잇는가 — 원장이 그 원장으로 시작하고 번호 대응이 그대로이며 다음 번호가 줄지 않았다."""
    if not new_parts["events.jsonl"].startswith(old_parts["events.jsonl"]):
        return False
    new_boxes = {e["name"]: e for e in new_manifest.get("outboxes", [])}
    for e in old_manifest.get("outboxes", []):
        n = new_boxes.get(e["name"])
        if n is None or int(n["next_n"]) < int(e["next_n"]) or n.get("door") != e.get("door"):
            return False
        new_index = {i["n"]: i for i in n.get("index", [])}
        for i in e.get("index", []):
            if new_index.get(i["n"]) != i:
                return False
    return True


def _verify_bundle(body: dict, pubkey_hex: str, max_bytes: int | None) -> tuple[bytes, dict, dict]:
    """상태 칸의 답 → (묶음, 목록, 조각). 지문과 서명을 **자기 공개키**로 본다. 묶음 안의 것을 믿지 않는다."""
    try:
        blob = base64.b64decode(body["blob_b64"], validate=True)
    except (KeyError, ValueError):
        raise StateError("상태 칸의 답에 묶음이 없다") from None
    if _sha(blob) != body.get("sha256"):
        raise StateError("받은 묶음의 지문이 서버가 말한 것과 다르다")
    try:
        ok = sp.verify(bytes.fromhex(body.get("sig", "")), hashlib.sha256(blob).digest(),
                       bytes.fromhex(pubkey_hex))
    except ValueError:
        ok = False
    if not ok:
        raise StateError("묶음의 서명이 자기 공개키로 서지 않는다")
    manifest, parts = unpack(blob, max_bytes=max_bytes)
    if manifest.get("generation") != body.get("generation"):
        raise StateError("묶음 안의 세대가 서버가 말한 세대와 다르다")
    return blob, manifest, parts


# ── 체크포인트 ───────────────────────────────────────────────────────────────

def _confirmed_bodies(outboxes: dict[str, Path], doors: dict[str, str], drop_url: str, token: str,
                      timeout: int) -> dict[str, set[str]]:
    """뺄 본문 — 드롭에 같은 봉투 지문과 같은 본문 지문으로 있고 **표지가 있는** 번호만.
    표지를 쓰지 않는 배치에서는 색인이 말하는 것이 응답한 인스턴스에 있다는 것뿐이므로 빼지 않는다."""
    out: dict[str, set[str]] = {}
    for name, door in doors.items():
        index = fetch_index(f"{drop_url.rstrip('/')}/v0/{door}", token, timeout=timeout)
        if index is None:
            continue
        local = _local_fingerprints(Path(outboxes[name]))
        out[name] = {e["n"] for e in index
                     if e.get("mirrored") is True and e["n"] in local
                     and local[e["n"]]["envelope_sha256"] == e["envelope_sha256"]
                     and local[e["n"]]["body_sha256"] is not None
                     and local[e["n"]]["body_sha256"] == e.get("body_sha256")}
    return out


def checkpoint(hub_dir, outboxes: dict[str, Path], doors: dict[str, str], *, seed: bytes,
               drop_url: str, token: str, onto: tuple[int, str] | None = None,
               dry_run: bool = False, with_body: bytes | None = None,
               wait: float = MARK_WAIT_SECONDS, timeout: int = hd.CLIENT_TIMEOUT_SECONDS,
               received_only: bool = False, sleep=time.sleep, clock=time.monotonic) -> dict:
    """묶음을 상태 칸에 올리고 표지를 본다. 이 명령은 편지를 올리지 않는다 — 끝난 뒤에 `push`한다.

    순서: 잠금 → 문 색인으로 뺄 본문을 정함 → 연장인지 봄 → 묶음을 뜨고 서명 → **보내기 전에** 기록 →
    조건(지금 세대와 지문)을 걸어 올림 → 표지를 기다림. 409는 자기 기록으로 가른다. 스스로 다시 올리는
    것은 서버의 지금 세대와 지문이 자기가 200을 받은 기록에 있을 때뿐이다.

    `ready_to_push`는 표지를 본 때에만 참이다. 표지를 쓰지 않는 서버의 200은 받았다는 뜻뿐이라
    참이 되지 않는다(Orin 053 C). 그런 배치에서 200만으로 나아가려면 `received_only`를 명시한다 —
    그때도 `ready_to_push`는 거짓이고 답에 `received_only`가 따로 실린다."""
    hub = Path(hub_dir)
    state_url = f"{drop_url.rstrip('/')}/v0/state"
    pubkey = sp.public_key(seed).hex()
    with ho.hub_write_lock(hub):
        rec = load_record(hub)
        rec["outboxes"] = {n: {"dir": str(Path(outboxes[n])), "door": doors.get(n)} for n in outboxes}
        last = rec["acked"][-1] if rec.get("acked") else None
        if dry_run:
            return _dry_run(hub, outboxes, doors, rec, with_body)
        if last is not None:
            _check_extension(hub, outboxes, doors, last)
        unchanged = None
        if last is not None and onto is None:
            now_state = _ledger_summary(hub, outboxes, doors)
            if all(now_state[k] == last.get(k) for k in _SUMMARY_KEYS):
                # 지난번에 받아들여진 뒤로 달라진 것이 없다. 다시 뜨지 않고 그 세대의 표지를 이어서
                # 본다 — 표지를 기다리다 끝난 뒤 같은 명령을 다시 부른 경우다.
                st, meta = _http(f"{state_url}?meta=1", token, timeout=timeout)
                if st == 200 and (meta.get("generation"), meta.get("sha256")) == \
                        (last["generation"], last["sha256"]):
                    unchanged = last
        omit = {} if unchanged else _confirmed_bodies(outboxes, doors, drop_url, token, timeout)

        def upload(expect: tuple[int, str], supersedes=()) -> dict:
            floor = max(int(rec.get("floor") or 0), expect[0])
            predicted = max(expect[0], floor) + 1
            pending = rec.get("pending")
            if pending and (pending["expect_generation"], pending["expect_sha256"]) == expect \
                    and pending.get("events_sha256") == _ledger_summary(hub, outboxes, doors)["events_sha256"]:
                # 응답을 못 받은 지난 시도: **적어 둔 그 바이트**를 같은 조건으로 다시 보낸다.
                # 다시 뜨면 지문이 달라져 남의 쓰기처럼 보인다.
                blob = base64.b64decode(pending["blob_b64"])
                req = {k: pending[k] for k in ("expect_generation", "expect_sha256", "floor_generation",
                                               "sha256", "sig")}
                req["blob_b64"] = pending["blob_b64"]
                predicted = pending["generation"]
                sent = {k: pending.get(k) for k in _SUMMARY_KEYS}
            else:
                blob = snapshot(hub, outboxes, doors=doors, omit_bodies=omit, generation=predicted,
                                prev_sha256=expect[1], supersedes=supersedes, _locked=True)
                limit = rec.get("max_bytes") or hd.STATE_MAX_BYTES
                if len(blob) > limit:
                    raise CheckpointStopped(
                        "too_large", f"묶음이 상태 칸의 한도({limit} 바이트)를 넘는다 — 올리지 않는다",
                        size=len(blob), max_bytes=limit)
                unpack(blob)                                 # 푼 크기의 상한까지 스스로 본다
                sig = sp.sign(hashlib.sha256(blob).digest(), seed).hex()
                req = {"expect_generation": expect[0], "expect_sha256": expect[1],
                       "floor_generation": floor, "sha256": _sha(blob),
                       "blob_b64": base64.b64encode(blob).decode("ascii"), "sig": sig}
                sent = _ledger_summary(hub, outboxes, doors)
                rec["pending"] = {**req, "generation": predicted, **sent}
                # 서버가 매길 번호를 보내기 전에 적는다. 응답을 받지 못해도 다음 floor에 들어간다.
                rec["floor"] = max(floor, predicted)
                _save_record(hub, rec)
            st, body = _http(state_url, token, req, timeout=timeout)
            return {"status": st, "body": body, "sha256": req["sha256"], "predicted": predicted,
                    "sent": sent}

        def accept(res: dict) -> int:
            """200만 받아들인다. 받아들여진 세대를 기록에 적는다 — 지금의 디스크가 아니라 **보낸 그 묶음**이
            아는 원장과 번호로. 다시 보낸 지난 시도의 바이트는 그 뒤에 달라진 발신함을 모른다(Jdot HQ D1)."""
            if res["status"] != 200:
                if res["status"] == 409:
                    raise CheckpointStopped("conflict", "조건이 지금 세대와 다르다 — 올리지 않는다",
                                            generation=res["body"].get("generation"),
                                            sha256=res["body"].get("sha256"))
                if res["status"] == 413:
                    raise CheckpointStopped("too_large", "서버가 묶음이 크다고 답했다",
                                            max_bytes=res["body"].get("max_bytes"))
                raise StateError(f"상태 칸에 올리지 못했다: HTTP {res['status']} "
                                 f"{res['body'].get('error', '')}")
            # 새 세대의 번호는 서버의 답에서 읽는다. 조건으로 낸 번호에 1을 더한 것이 아닐 수 있다.
            gen = res["body"]["generation"]
            rec["acked"].append({"generation": gen, "sha256": res["sha256"], **res["sent"]})
            rec["floor"] = max(int(rec.get("floor") or 0), gen)
            rec["pending"] = None
            _save_record(hub, rec)
            return gen

        if unchanged is not None:
            generation, done_sha, res = unchanged["generation"], unchanged["sha256"], None
        else:
            # 조건: 내가 아는 지금 세대와 그 지문
            if onto is not None:
                expect, supersedes = onto, (onto[1],) if onto[0] else ()
            elif last is not None:
                expect, supersedes = (last["generation"], last["sha256"]), ()
            else:
                st, meta = _http(f"{state_url}?meta=1", token, timeout=timeout)
                if st == 404 and meta.get("generation") == 0:
                    expect, supersedes = (0, ""), ()
                elif st == 200:
                    # 기록이 없는데 칸이 차 있다. 가장 새 세대의 번호만 묻고 자기 것을 올리면 낡은 사본이
                    # 새 상태를 밀어낸다 — 스스로 올리지 않는다.
                    raise CheckpointStopped(
                        "no_record_slot_not_empty",
                        "기록이 없는데 상태 칸이 차 있다 — 스스로 올리지 않는다. 그 세대에서 복원하거나, "
                        "견준 뒤 --onto <세대>:<지문>으로 명시한다",
                        generation=meta.get("generation"), sha256=meta.get("sha256"))
                else:
                    raise StateError(f"상태 칸을 물을 수 없다: HTTP {st} {meta.get('error', '')}")
                if meta.get("max_bytes"):
                    rec["max_bytes"] = meta["max_bytes"]

            res = upload(expect, supersedes)
            if res["status"] == 409 and onto is None:
                cur = (res["body"].get("generation"), res["body"].get("sha256"))
                known = _acked(rec, cur[0], cur[1]) if cur[0] else None
                if known is None and cur != (0, ""):
                    _raise_409(hub, outboxes, doors, state_url, token, pubkey, cur, timeout)
                # 칸이 되돌아갔다 — 그 세대와 지문이 내가 200을 받은 기록에 있다(또는 빈 칸으로 돌아갔고
                # 나는 기록을 갖고 있다). 그 위에 다시 떠서 올린다. 묶음은 통째라 사이의 세대를 되올릴
                # 필요가 없다.
                rec["pending"] = None
                res = upload(cur)
                res["rebased_onto"] = cur[0]
            generation = accept(res)
            if res["sent"] != _ledger_summary(hub, outboxes, doors):
                # 받아들여진 것은 지난 시도의 바이트이고 그 뒤에 발신함이 달라졌다. 그 세대는 지금의
                # 발신함을 모른다. 그 위에 지금 상태를 떠서 한 번 더 올리고, 표지는 뒤의 세대에서 본다.
                extended_from = generation
                carried = {k: res[k] for k in ("rebased_onto",) if k in res}
                res = upload((generation, res["sha256"]))
                generation = accept(res)
                res.update(carried, extended_after_resend=extended_from)

    # 표지를 기다린다(잠금 밖). 표지를 본 뒤에만 편지를 올린다.
    if res is None:
        out = {"generation": generation, "sha256": done_sha, "dedup": False, "unchanged": True,
               "omitted_bodies": {}}
    else:
        done_sha = res["sha256"]
        out = {"generation": generation, "sha256": done_sha,
               "dedup": bool(res["body"].get("dedup")),
               "omitted_bodies": {k: sorted(v, key=int) for k, v in omit.items() if v}}
        for k in ("rebased_onto", "extended_after_resend"):
            if k in res:
                out[k] = res[k]
    out.update(_wait_mark(state_url, token, generation, done_sha, wait, timeout, sleep, clock,
                          received_only))
    return out


def _wait_mark(state_url, token, generation, sha, wait, timeout, sleep, clock,
               received_only: bool = False) -> dict:
    deadline = clock() + wait
    while True:
        st, meta = _http(f"{state_url}?meta=1", token, timeout=timeout)
        if st == 200 and (meta.get("generation"), meta.get("sha256")) == (generation, sha):
            if "mirrored" not in meta:
                # 표지를 쓰지 않는 배치: 200이 전부이고 그 뜻은 받음이다. 바깥에 옮겨졌다는 뜻이 아니므로
                # 올려도 된다고 답하지 않는다. 기다려도 표지는 오지 않는다.
                out = {"mirrored": None, "ready_to_push": False, "marks_absent": True}
                if received_only:
                    out["received_only"] = True
                return out
            if meta["mirrored"] is True:
                return {"mirrored": True, "ready_to_push": True}
        elif st in (200, 404):
            # 지금 세대가 방금 올린 것이 아니다. 칸이 되돌아갔거나 인스턴스가 바뀌었다 —
            # 다음에 부르면 기록으로 가른다. 편지를 올리지 않는다.
            return {"mirrored": False, "ready_to_push": False, "slot_moved": True,
                    "slot_generation": meta.get("generation")}
        if clock() >= deadline:
            return {"mirrored": False, "ready_to_push": False}
        sleep(_pause(st, meta, MARK_POLL_SECONDS))


def _raise_409(hub, outboxes, doors, state_url, token, pubkey, cur, timeout) -> None:
    """기록으로 정해지지 않은 409 — 서버의 지금 세대를 받아 자기 상태와 견주고 멈춘다."""
    st, body = _http(state_url, token, timeout=timeout)
    relation = "unknown"
    if st == 200:
        try:
            _blob, their_m, their_p = _verify_bundle(body, pubkey, None)
            mine = snapshot(hub, outboxes, doors=doors, allow_unexported=True, _locked=True)
            my_m, my_p = unpack(mine)
            if _extends(my_m, my_p, their_m, their_p):
                relation = "superseded"          # 내 것이 서버 것의 앞부분: 이어받아졌다
            elif _extends(their_m, their_p, my_m, my_p):
                relation = "uncertain"           # 서버 것이 내 것의 앞부분: 알 수 없다
            else:
                relation = "diverged"
        except StateError as e:
            relation = f"unverifiable: {e}"
    messages = {
        "superseded": "다른 주체가 내 상태를 이어받아 일을 더 했다. 저장하려던 것은 그 안에 살아 있다 — "
                      "멈춘다. 새로 쓰지 않고 다시 서명하지 않는다",
        "uncertain": "서버의 세대가 내 상태의 앞부분이지만 내 기록에 없다 — 스스로 다시 올리지 않는다. "
                     "견준 결과를 보고 --onto <세대>:<지문>으로 명시한다",
        "diverged": "갈라졌다 — 멈춘다. 편지를 올리지 않는다",
    }
    raise CheckpointStopped(relation.split(":")[0],
                            messages.get(relation, f"서버의 세대를 확인할 수 없다({relation}) — 멈춘다"),
                            generation=cur[0], sha256=cur[1])


def _dry_run(hub: Path, outboxes: dict[str, Path], doors: dict[str, str], rec: dict,
             with_body: bytes | None) -> dict:
    """올리지 않고 묶음의 크기만 본다. `with_body`는 아직 원장에 넣지 않은 본문 — 넣기 전에 잰다."""
    blob = snapshot(hub, outboxes, doors=doors, allow_unexported=True, _locked=True)
    manifest, parts = unpack(blob)
    extra = len(zlib.compress(with_body, 9)) if with_body is not None else 0
    limit = rec.get("max_bytes") or hd.STATE_MAX_BYTES
    return {"dry_run": True, "size": len(blob), "with_body_estimate": len(blob) + extra,
            "max_bytes": limit, "fits": len(blob) + extra <= limit,
            "note": "본문을 모두 실은 크기다. 표지가 있는 편지의 본문은 올릴 때 빠진다"}


def body_fits(hub_dir, body: bytes, *, _locked: bool = False) -> dict | None:
    """상태 칸을 쓰는 원장인가, 그렇다면 이 본문이 묶음에 들어가는가 — `message`가 원장에 넣기 전에 본다.
    넣은 뒤에는 되돌릴 수 없다. 상태 칸을 쓰지 않는 원장이면 None."""
    hub = Path(hub_dir)
    rec = load_record(hub)
    if not rec.get("acked") and not rec.get("pending"):
        return None
    outboxes = {n: Path(v["dir"]) for n, v in (rec.get("outboxes") or {}).items()
                if Path(v["dir"]).is_dir()}
    doors = {n: v["door"] for n, v in (rec.get("outboxes") or {}).items() if v.get("door")}
    doors = {n: d for n, d in doors.items() if n in outboxes}
    if _locked:
        return _dry_run(hub, outboxes, doors, rec, body)
    with ho.hub_write_lock(hub):
        return _dry_run(hub, outboxes, doors, rec, body)


# ── 상태 칸에서 되살리기 ─────────────────────────────────────────────────────

def restore_from_slot(hub_dir, outbox_dirs: dict[str, Path], *, drop_url: str, token: str,
                      pubkey_hex: str, generation: int | None = None, accept_timeout: bool = False,
                      wait: float = RESTORE_WAIT_SECONDS, timeout: int = hd.CLIENT_TIMEOUT_SECONDS,
                      received_only: bool = False, sleep=time.sleep, clock=time.monotonic) -> dict:
    """상태 칸의 세대에서 되살린다.

    1. 묶음을 실은 **그 응답에서** `settled`와 `mirrored`가 참이어야 그 묶음을 쓴다. 한 응답은 한
       인스턴스가 낸다. 재배포 직후의 인스턴스는 낡은 세대를 내줄 수 있다.
    2. 남겨 둔 세대를 증인으로 본다. 받은 세대가 표지 있는 다른 남은 세대를 모두 이어야 한다.
       잇지 않으면 갈라진 것이고 되살리지 않는다.
    3. 복원한다. 받은 세대와 지문을 기록의 첫 줄로 적는다.
    `generation`을 주면 사람이 고른 세대다 — 증인 확인을 하지 않는다.

    표지를 쓰지 않는 서버는 `settled`도 `mirrored`도 주지 않는다. 그 서버의 묶음은 기본으로 받지
    않는다(Orin 053 C). `received_only`를 명시하면 200만 믿고 받고, 보고에 `received_only`가 실린다."""
    state_url = f"{drop_url.rstrip('/')}/v0/state"
    url = state_url if generation is None else f"{state_url}?generation={generation}"
    deadline = clock() + wait
    while True:
        st, body = _get_patient(url, token, timeout, sleep, clock, deadline)
        if st == 404:
            raise StateError("상태 칸에 그 세대가 없다" if generation is not None else "상태 칸이 비었다")
        if st != 200:
            raise StateError(f"상태 칸을 읽지 못했다: HTTP {st} {body.get('error', '')}")
        if "settled" not in body:
            # 표지를 쓰지 않는 배치 — 자리를 잡았는지도 바깥에 옮겨졌는지도 알 길이 없다.
            if not received_only:
                raise StateError(
                    "이 서버는 표지를 쓰지 않는다 — 자리를 잡았는지도 그 세대가 바깥에 옮겨졌는지도 "
                    "알 수 없다. 되살리지 않는다. 200만 믿고 받으려면 --received-only로 명시한다")
            waiting = None
        elif body.get("settled") is not True and not (accept_timeout and body.get("settled_by_timeout")):
            waiting = ("인스턴스가 시한으로만 자리를 잡았다 — 앞 인스턴스가 멈춘 것을 확인한 뒤 "
                       "--accept-timeout으로 받는다" if body.get("settled_by_timeout")
                       else "인스턴스가 아직 자리를 잡지 않았다")
            if body.get("settled_by_timeout"):
                raise StateError(waiting)
        elif body.get("mirrored") is not True:
            waiting = "그 세대에 표지가 없다 — 바깥에 옮겨지지 않았거나 다른 바이트와 어긋났다"
        else:
            waiting = None
        if waiting is None:
            break
        if clock() >= deadline:
            raise StateError(f"{waiting}. 정한 시간 안에 풀리지 않았다 — 되살리지 않는다")
        sleep(RESTORE_POLL_SECONDS)
    max_bytes = body.get("max_bytes")
    blob, manifest, parts = _verify_bundle(body, pubkey_hex, max_bytes)
    witnesses = []
    if generation is None:
        superseded = set(manifest.get("supersedes") or [])
        for k in body.get("kept") or []:
            if k["generation"] == body["generation"] or k["sha256"] in superseded:
                continue
            if "mirrored" in k and k["mirrored"] is not True:
                continue                                 # 표지가 없는 남은 세대는 증인으로 쓰지 않는다
            st2, other = _get_patient(f"{state_url}?generation={k['generation']}", token, timeout,
                                      sleep, clock, deadline)
            if st2 == 404:
                continue                                 # 그 사이에 지워졌다
            if st2 != 200:
                raise StateError(f"남겨 둔 세대 {k['generation']}을 받지 못했다: HTTP {st2}")
            _b, wm, wp = _verify_bundle(other, pubkey_hex, max_bytes)
            if not _extends(wm, wp, manifest, parts):
                raise StateError(
                    f"갈라졌다 — 지금 세대 {body['generation']}이 남겨 둔 세대 {k['generation']}을 잇지 "
                    "않는다. 되살리지 않는다. 이을 세대를 사람이 고르고 --generation으로 받는다")
            witnesses.append(k["generation"])
    report = restore(blob, hub_dir, outbox_dirs, max_bytes=max_bytes)
    rec = load_record(hub_dir)
    outs = {n: Path(d) for n, d in outbox_dirs.items()}
    rec["acked"] = [{"generation": body["generation"], "sha256": body["sha256"],
                     **_ledger_summary(Path(hub_dir), outs,
                                       {e["name"]: e.get("door")
                                        for e in manifest.get("outboxes", [])})}]
    rec["floor"] = max(body["generation"], max((k["generation"] for k in body.get("kept") or []),
                                               default=0))
    rec["max_bytes"] = max_bytes
    rec["outboxes"] = {e["name"]: {"dir": str(Path(outbox_dirs[e["name"]])), "door": e.get("door")}
                       for e in manifest.get("outboxes", [])}
    _save_record(hub_dir, rec)
    report.update(generation=body["generation"], sha256=body["sha256"], witnesses=witnesses,
                  settled=body.get("settled"), mirrored=body.get("mirrored"))
    if "settled" not in body:
        report["received_only"] = True
    return report


def fetch_bodies(outbox_dir, door_url: str, token: str, numbers: list[str], *,
                 timeout: int = hd.CLIENT_TIMEOUT_SECONDS) -> dict:
    """되살린 발신함에 빠진 본문을 드롭의 문에서 받아 채운다. **쓰기 전에** 봉투의 지문과 대조하고,
    다르면 쓰지 않는다."""
    out = Path(outbox_dir)
    want = set(numbers)
    filled, failed = [], []
    since = "000"
    while want:
        page = hd.fetch_page(door_url, token, since, timeout=timeout)
        for q in page["quads"]:
            n = q["n"]
            since = n
            if n not in want:
                continue
            want.discard(n)
            env_p = out / f"{n}-envelope.json"
            digest = _strict_json(env_p.read_bytes()).get("payload", {}).get("body_sha256") \
                if env_p.is_file() else None
            body = base64.b64decode(q["body_b64"]) if q.get("body_name") else None
            if (body is None or digest is None or _sha(body) != digest
                    or not _BODY_FILE_RE.match(q["body_name"])
                    or base64.b64decode(q["envelope_b64"]) != env_p.read_bytes()):
                failed.append(n)
                continue
            target = out / f"{n}-{q['body_name']}"
            if not target.exists():
                target.write_bytes(body)
            filled.append(n)
        if not page.get("more") or not page["quads"]:
            break
    return {"filled": filled, "failed": sorted(failed + sorted(want), key=int)}
