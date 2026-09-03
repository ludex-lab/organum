"""organum-bbs — 게시판·전화번호부 의미 투영 CLI (0.5.0).

CLI와 라이브러리가 의미 투영을 각자 재구현하지 않는다(Orin 037): 모든 동사가
`organum.bbs`의 같은 core 함수를 호출한다. 입력은 이벤트 스트림 JSON(파일 또는
stdin), 출력은 결정적 JSON. malformed 입력은 nonzero exit로 수렴한다.

동사:
  board <events.json>              board 상태+스레드 투영
  directory <events.json>          전화번호부 컴파일(claimed core — verified 미발행)
  profile <events.json>           주체별 현재 프로필 투영
  digest <events.json> [--since T]  다이제스트 투영(반가설)

기본 제품 경로는 claimed core다 — voice verified 등급은 실험 경계의 opt-in이며
이 CLI는 verifier를 주입하지 않는다(subject_authority_verified 미발행).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from organum import bbs


def _load_events(path: str) -> list[dict[str, Any]]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(
        encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("이벤트 스트림은 JSON 배열이어야 한다")
    return data


def _emit(obj: Any) -> int:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_board(a) -> int:
    st = bbs.project_board(_load_events(a.events))
    return _emit({"board": st["board"], "status": st["status"],
                  "caretaker": st["caretaker"], "flags": st["flags"],
                  "scale": st["scale"], "metadata": st["metadata"],
                  "members": sorted(f"{l}/{i}" for l, i in st["members"]),
                  "posts": [p["post_id"] for p in st["posts"]],
                  "threads": bbs.threads(st),
                  "notices": [n["notice_id"] for n in st["notices"]],
                  "rejected": st["rejected"], "last_event_at": st["last_event_at"]})


def _cmd_directory(a) -> int:
    rows = bbs.compile_people_directory(_load_events(a.events),
                                        compiled_at=a.compiled_at or "")
    return _emit(rows)


def _cmd_profile(a) -> int:
    st = bbs.project_profiles(_load_events(a.events))
    return _emit({"rows": [st["rows"][c] for c in sorted(st["rows"])],
                  "withdrawn": sorted(f"{l}/{i}/{e}" for l, i, e
                                      in st["withdrawn"]),
                  "rejected": st["rejected"]})


def _cmd_digest(a) -> int:
    return _emit(bbs.project_digest(_load_events(a.events), since=a.since))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="organum-bbs", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, extra in [("board", _cmd_board, None),
                            ("directory", _cmd_directory, "compiled_at"),
                            ("profile", _cmd_profile, None),
                            ("digest", _cmd_digest, "since")]:
        p = sub.add_parser(name)
        p.add_argument("events", help="이벤트 스트림 JSON 파일(또는 - = stdin)")
        if extra == "compiled_at":
            p.add_argument("--compiled-at", default=None, dest="compiled_at")
        if extra == "since":
            p.add_argument("--since", default=None)
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError,
            FileNotFoundError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
