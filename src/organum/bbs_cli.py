"""organum-bbs — 게시판·전화번호부 클라이언트 CLI (0.6.0).

CLI와 라이브러리가 의미 투영을 각자 재구현하지 않는다(Orin 037): 모든 동사가
`organum.bbs`(투영)·`organum.bbs_wire`(수거·검증·게시)의 같은 core 함수를 호출한다.
출력은 결정적 JSON. malformed 입력·계약 위반·전송 실패는 nonzero exit로 수렴한다.

파일 입력 동사(0.5.0, 오프라인 투영 — 그대로):
  board <events.json>                 board 상태+스레드 투영
  directory <events.json>             전화번호부 컴파일(claimed core — verified 미발행)
  profile <events.json>               주체별 현재 프로필 투영
  digest <events.json> [--since T]    다이제스트 투영(반가설)

드롭 동사(0.6.0, Orin 040 — 서버 hunk 0):
  boards --url BASE --token-file T    서버 채널 트리 + 내용으로 추정한 종류(inferred)
  pull CHANNEL --url BASE --token-file T --tree DIR
                                      문 전부 수거(회차 워밍 1회) → DIR/CHANNEL/from-*/
                                      회차 상태 DIR/CHANNEL/.round.json
  read CHANNEL --tree DIR --hub HUBDIR [--as board|directory] [--since AT]
       [--after lab:x:n] [--compiled-at AT] [--allow-problems]
                                      **오프라인·결정적** 읽기(서명·digest·shape 검증 →
                                      (at, lab, n) 정렬 → 투영). transport_problems나
                                      round_problems(실패·미시도·미완성·닫히지 않은 회차)가
                                      있으면 exit 1(--allow-problems로만 0).
  post CHANNEL --event EV.json --hub HUBDIR --key SEED --signer S --key-id K
       --epoch E --url BASE --token-file T --outbox DIR --to-lab L [--to-id board]
                                      의미 검증 → 서명·자기 원장 → outbox quad 확정 →
                                      push. 응답 유실/timeout/5xx는 status=unknown +
                                      재시도 좌표. `post --retry N`은 같은 번호·같은
                                      bytes 재push(새 message 금지).

기본 제품 경로는 claimed core다 — voice verified 등급은 실험 경계의 opt-in이며
이 CLI는 verifier를 주입하지 않는다(subject_authority_verified 미발행). 멤버십은
서버가 아니라 투영이 판정한다 — 비멤버 게시는 저장되되 read에서 거부로 보인다.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

from organum import bbs
from organum import bbs_wire as bw
from organum import hub_drop as hd
from organum import hub_ops as ho


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


def _doors(arg: str | None) -> list[str] | None:
    if not arg:
        return None
    return [d if d.startswith("from-") else f"from-{d}"
            for d in (x.strip() for x in arg.split(",")) if d]


# ── 파일 입력 동사(0.5.0) ────────────────────────────────────────────────────

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


# ── 드롭 동사(0.6.0) ─────────────────────────────────────────────────────────

def _cmd_boards(a) -> int:
    token = hd.load_tokens(a.token_file)[0]
    st: dict = {}
    channels = bw.discover(a.url, token, timeout=a.timeout, warmup=not a.no_warmup,
                           probe=not a.no_probe, stats=st)
    return _emit({"channels": channels, "source": "server",
                  "kinds_are": "inferred — 서명 검증 전 내용 관측; 구독은 로컬 선택",
                  **{k: st[k] for k in ("warm_ms", "warm_ok", "warm_budget_s",
                                        "probe_gets") if k in st}})


def _cmd_pull(a) -> int:
    token = hd.load_tokens(a.token_file)[0]
    st: dict = {}
    rnd = bw.pull_channel(a.url, token, a.channel, a.tree, doors=_doors(a.doors),
                          timeout=a.timeout, warmup=not a.no_warmup,
                          round_at=a.round_at or ho.now_z(), stats=st)
    _emit(rnd)
    return 0 if all(r.get("ok") for r in rnd["results"].values()) \
        and not rnd["untried"] else 1


def _cmd_read(a) -> int:
    _, _, hub = ho.load_hub(a.hub)                       # 읽기 전용 replay
    if a.as_kind == "directory":
        if not a.compiled_at:
            raise ValueError("--as directory에는 --compiled-at이 필요하다(read는 시각을 "
                             "지어내지 않는다)")
        out = bw.read_directory(a.tree, a.channel, hub, compiled_at=a.compiled_at,
                                doors=_doors(a.doors))
    else:
        out = bw.read_board(a.tree, a.channel, hub, since=a.since, after=a.after,
                            doors=_doors(a.doors))
    out["offline"] = True
    _emit(out)
    partial = out["transport_problems"] or out["completeness"] == "partial"
    if partial and not a.allow_problems:
        return 1        # 파일 검증 문제 또는 부분 갱신(여울 085 §1·041 R4) — 조용히 0 금지
    return 0            # completeness=unknown(회차 기록 없는 미러)은 표시만, exit 0


def _cmd_post(a) -> int:
    token = hd.load_tokens(a.token_file)[0]
    st: dict = {}
    if a.retry:
        r = bw.push_outbox(a.outbox, a.channel, a.retry, token, timeout=a.timeout,
                           warmup=not a.no_warmup, attempt_at=ho.now_z(), stats=st)
    else:
        for req in ("event", "hub", "key", "signer", "key_id", "to_lab"):
            if not getattr(a, req):
                raise ValueError(f"--{req.replace('_', '-')}는 필수다(--retry가 아니면)")
        event = json.loads(Path(a.event).read_text(encoding="utf-8"))
        rec = bw.prepare_post(hub_dir=a.hub, seed_path=a.key, signer=a.signer,
                              key_id=a.key_id, epoch=a.epoch, board=a.channel,
                              event=event, url_base=a.url, to_lab=a.to_lab,
                              to_id=a.to_id, to_epoch=a.to_epoch, outbox=a.outbox)
        r = bw.push_outbox(a.outbox, a.channel, rec["n"], token, timeout=a.timeout,
                           warmup=not a.no_warmup, attempt_at=ho.now_z(), stats=st)
    _emit(r)
    return 0 if r["status"] == "stored" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="organum-bbs", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, extra in [("board", _cmd_board, None),
                            ("directory", _cmd_directory, "compiled_at"),
                            ("profile", _cmd_profile, None),
                            ("digest", _cmd_digest, "since")]:
        p = sub.add_parser(name)
        p.add_argument("events", help="이벤트 배열 JSON 파일(또는 - = stdin)")
        if extra == "compiled_at":
            p.add_argument("--compiled-at", default=None, dest="compiled_at")
        if extra == "since":
            p.add_argument("--since", default=None)
        p.set_defaults(func=fn)

    def net(p):
        p.add_argument("--url", required=True, help="드롭 base URL(예 https://host)")
        p.add_argument("--token-file", required=True)
        p.add_argument("--timeout", type=int, default=hd.CLIENT_TIMEOUT_SECONDS)
        p.add_argument("--no-warmup", action="store_true")

    p = sub.add_parser("boards"); net(p)
    p.add_argument("--no-probe", action="store_true",
                   help="첫 페이지 탐색 없이 트리만(종류는 전부 unknown)")
    p.set_defaults(func=_cmd_boards)

    p = sub.add_parser("pull"); p.add_argument("channel"); net(p)
    p.add_argument("--tree", required=True, help="로컬 append-only 트리 루트")
    p.add_argument("--doors", default=None, help="쉼표 목록(생략=서버 트리)")
    p.add_argument("--round-at", default=None, dest="round_at")
    p.set_defaults(func=_cmd_pull)

    p = sub.add_parser("read"); p.add_argument("channel")
    p.add_argument("--tree", required=True)
    p.add_argument("--hub", required=True, help="registry 재생용 hub 디렉터리(무접촉)")
    p.add_argument("--as", dest="as_kind", choices=("board", "directory"),
                   default="board")
    p.add_argument("--since", default=None, help="표시 필터: at > SINCE(상태는 전체)")
    p.add_argument("--after", default=None, help="표시 필터: lab:<name>:<n> 뒤")
    p.add_argument("--compiled-at", default=None, dest="compiled_at")
    p.add_argument("--doors", default=None)
    p.add_argument("--allow-problems", action="store_true", dest="allow_problems")
    p.set_defaults(func=_cmd_read)

    p = sub.add_parser("post"); p.add_argument("channel"); net(p)
    p.add_argument("--outbox", required=True,
                   help="durable outbox 루트(랩당 하나) — 채널별 out-<channel>/ 하위")
    p.add_argument("--retry", default=None, help="outbox quad 번호 — 같은 bytes 재push")
    p.add_argument("--event", default=None, help="이벤트 JSON object 파일")
    p.add_argument("--hub", default=None)
    p.add_argument("--key", default=None)
    p.add_argument("--signer", default=None)
    p.add_argument("--key-id", default=None, dest="key_id")
    p.add_argument("--epoch", type=int, default=1)
    p.add_argument("--to-lab", default=None, dest="to_lab")
    p.add_argument("--to-id", default="board", dest="to_id")
    p.add_argument("--to-epoch", type=int, default=1, dest="to_epoch")
    p.set_defaults(func=_cmd_post)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError,
            OSError, hd.DropError, urllib.error.URLError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
