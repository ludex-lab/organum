"""organum CLI. 출력 계약: docs/format-v0.md §6, guard 계약: §7."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from organum import FORMAT_VERSION, __version__
from organum import checkup as checkup_mod
from organum import distill as distill_mod
from organum import migrate as migrate_mod
from organum import guard, memory, reflect
from organum import state as st


def _require_state() -> Path:
    return st.require_state_dir(Path.cwd())


def _writable_meta(state_dir: Path) -> dict:
    """쓰기 명령의 버전 게이트 (§10: 구버전 디렉터리에 쓰기 전 migrate 필수)."""
    meta = st.load_meta(state_dir)
    st.check_format_version(meta)  # 미래 버전이면 여기서 거부
    if meta.get("format_version") != FORMAT_VERSION:
        raise SystemExit("organum: 구버전 포맷입니다 — 쓰기 전에 'organum migrate'를 실행하세요.")
    return meta


def _forid(args: argparse.Namespace) -> str | None:
    """세포 id 해석: --for 우선, 없으면 ORGANUM_CELL 환경변수 (organum join이 잡아둠). 없으면 None=owner."""
    return getattr(args, "for_id", None) or os.environ.get("ORGANUM_CELL") or None


def _need_forid(args: argparse.Namespace) -> str:
    """id가 필수인 명령(relay·agora·roster)용 — 없으면 명확히 거부."""
    cid = _forid(args)
    if not cid:
        raise SystemExit("organum: 세포 id가 필요합니다 — '--for <id>' 또는 'export ORGANUM_CELL=<id>' (organum join이 잡아줍니다).")
    return cid


def _from(args: argparse.Namespace) -> str:
    """발신 이름: --from 우선, 없으면 ORGANUM_CELL(join이 잡은 정체성), 그것도 없으면 'cell'.
    익명 'cell' 게시 방지 — 피어평가·조율 귀속엔 발신 정체성이 필요하다."""
    return getattr(args, "frm", None) or os.environ.get("ORGANUM_CELL") or "cell"


def cmd_init(args: argparse.Namespace) -> int:
    project_root = Path.cwd()
    try:
        state_dir, repo_map = st.init_state_dir(project_root, args.agent)
    except FileExistsError as e:
        print(
            f"organum: {e}가 이미 있습니다. 점검은 'organum checkup', 복원은 'organum restore'.",
            file=sys.stderr,
        )
        return 1

    n_files = sum(1 for n in repo_map["nodes"].values() if n["kind"] == "file")
    print(f"initialized {state_dir}")
    print(
        f"  agent: {args.agent} · map: {n_files} file{'s' if n_files != 1 else ''} "
        f"({repo_map['seed_source']})"
    )
    if repo_map["seed_source"] == "none":
        print("  주의: git 저장소가 아니라 지도를 시드하지 못했습니다.", file=sys.stderr)
    print("다음: 'organum context' 출력을 에이전트의 시스템 컨텍스트에 주입하세요.")
    return 0


def _self_block(state_dir: Path, agent: str) -> str | None:
    path = state_dir / "self.md"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    # 제목 아래 전체를 섹션 단위로 자르고, 빈 섹션(공백/HTML 주석뿐)은 생략
    head: list[str] = []  # 제목과 첫 섹션 사이 (Last reflection 줄)
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            current = [line]
            sections.append(current)
        elif current is None:
            head.append(line)
        else:
            current.append(line)

    def is_content(line: str) -> bool:
        s = line.strip()
        return bool(s) and not (s.startswith("<!--") and s.endswith("-->"))

    kept = [s for s in sections if any(is_content(l) for l in s[1:])]
    if not kept:
        return None
    body = [l for l in head if is_content(l)]
    for s in kept:
        body.extend([""] + [l for l in s if is_content(l) or l == s[0]])
    return f"[Self: {agent}]\n" + "\n".join(body).strip()


def _map_block(state_dir: Path) -> str | None:
    repo_map = st.load_repo_map(state_dir)
    if repo_map is None or not repo_map["nodes"]:
        return None
    files = {p: n for p, n in repo_map["nodes"].items() if n["kind"] == "file"}
    read = {p: n for p, n in files.items() if n.get("status") == "read"}
    frontier = [p for p, n in files.items() if n.get("status") != "read"]

    out = [f"[Map] files {len(files)} · read {len(read)} · frontier {len(frontier)}"]
    if read:
        entries = [p + (f" ({n['note']})" if n.get("note") else "") for p, n in read.items()]
        out.append("read: " + " · ".join(entries))
    if frontier:
        # 디렉터리 단위로 묶어 요약 — frontier 줄이 핵심 신호다 (§6)
        by_top: dict[str, int] = {}
        singles: list[str] = []
        for p in frontier:
            if "/" in p:
                top = p.split("/", 1)[0] + "/"
                by_top[top] = by_top.get(top, 0) + 1
            else:
                singles.append(p)
        groups = [f"{d} ({k} file{'s' if k != 1 else ''})" for d, k in sorted(by_top.items())]
        out.append("frontier: " + " · ".join(groups + sorted(singles)))
    return "\n".join(out)


def _wm_blocks(state_dir: Path) -> list[str]:
    blocks = []
    wm_dir = state_dir / "worldmodel"
    if not wm_dir.is_dir():
        return blocks
    for path in sorted(wm_dir.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        # front matter 제거
        if lines and lines[0] == "---":
            try:
                lines = lines[lines.index("---", 1) + 1 :]
            except ValueError:
                pass
        body = [l for l in lines if not l.strip().startswith("<!--")]
        has_content = any(
            l.strip() and not l.startswith("#") for l in body
        )
        if has_content:
            blocks.append(f"[WM: {path.stem}]\n" + "\n".join(body).strip())
    return blocks


def cmd_context(args: argparse.Namespace) -> int:
    state_dir = st.require_state_dir(Path.cwd())
    meta = st.load_meta(state_dir)
    warning = st.check_format_version(meta)
    if warning:
        print(f"organum: {warning}", file=sys.stderr)

    blocks = []
    soma = st.soma_dir(state_dir, _forid(args))  # [Self]=이 세포 soma · [Map]/[WM]=commons(루트)
    self_block = _self_block(soma, _forid(args) or meta.get("agent", "agent"))
    if self_block:
        blocks.append(self_block)
    map_block = _map_block(state_dir)
    if map_block:
        blocks.append(map_block)
    blocks.extend(_wm_blocks(state_dir))

    if blocks:
        print("\n\n".join(blocks))
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    state_dir = _require_state()
    _writable_meta(state_dir)
    soma = st.ensure_soma(state_dir, _forid(args))  # 게스트 세포면 자기 soma에 저장
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    verdict, mem_id = memory.remember(
        soma,
        args.content,
        mem_type=args.type,
        tags=tags,
        confidence=args.confidence,
        supersedes=args.supersedes,
    )
    if not verdict.ok:
        print(f"organum: guard 차단 ({verdict.rule}) — {verdict.reason}", file=sys.stderr)
        if guard.streak_active(soma):
            print(
                f"organum: STREAK — 연속 {guard.streak_count(soma)}회 차단. "
                "호스트/설정을 점검하세요 ('organum checkup').",
                file=sys.stderr,
            )
        return 2
    if verdict.decision == "flagged":
        print(f"organum: 저장됨(표시: {verdict.rule}) — {verdict.reason}", file=sys.stderr)
    print(mem_id)
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    state_dir = _require_state()
    soma = st.soma_dir(state_dir, _forid(args))  # 게스트 세포면 자기 soma 조회
    window = memory.parse_window(args.when)
    records = memory.recall_window(soma, window)
    print(memory.render_recall(records, args.when))
    return 0


def _resolve_map_path(project_root: Path, raw: str) -> str:
    p = Path(raw)
    abs_p = p if p.is_absolute() else (Path.cwd() / p)
    try:
        return abs_p.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        raise SystemExit(f"organum: {raw}는 프로젝트({project_root}) 안의 경로가 아닙니다.")


def cmd_map(args: argparse.Namespace) -> int:
    state_dir = _require_state()
    project_root = state_dir.parent
    repo_map = st.load_repo_map(state_dir)
    if repo_map is None:
        raise SystemExit("organum: map이 없습니다 — 'organum init'이 시드했어야 합니다.")

    sub = args.map_cmd or "view"
    if sub == "mark":
        _writable_meta(state_dir)
        rel = _resolve_map_path(project_root, args.path)
        node = repo_map["nodes"].get(rel)
        if node is None or node.get("kind") != "file":
            raise SystemExit(f"organum: map에 없는 파일입니다: {rel} — 새 파일이면 'organum map sync' 먼저.")
        node["status"] = "read"
        sha = st.git_blob_sha(project_root, rel)
        if sha:
            node["sha"] = sha
        if args.note:
            node["note"] = args.note
        st.write_json(state_dir / "map" / "repo.map.json", repo_map)
        print(f"read: {rel}" + (f" — {args.note}" if args.note else ""))
        return 0

    if sub == "sync":
        _writable_meta(state_dir)
        new_map, stats = st.sync_repo_map(project_root, repo_map)
        st.write_json(state_dir / "map" / "repo.map.json", new_map)
        print(f"sync: 신규 {stats['added']} · 소실 제거 {stats['removed']} · 총 {stats['total']} files")
        return 0

    files = {p: n for p, n in repo_map["nodes"].items() if n["kind"] == "file"}
    frontier = sorted(p for p, n in files.items() if n.get("status") != "read")
    if sub == "frontier":
        for p in frontier:
            print(p)
        return 0

    # view
    read = {p: n for p, n in sorted(files.items()) if n.get("status") == "read"}
    print(
        f"[Map] files {len(files)} · read {len(read)} · frontier {len(frontier)} "
        f"(seed: {repo_map.get('seed_source')} @ {repo_map.get('seeded_at')})"
    )
    if read:
        print("read:")
        for p, n in read.items():
            print(f"  {p}" + (f" — {n['note']}" if n.get("note") else ""))
    if frontier:
        print("frontier:")
        for p in frontier:
            print(f"  {p}")
    return 0


def cmd_distill(args: argparse.Namespace) -> int:
    state_dir = _require_state()
    _writable_meta(state_dir)
    if args.from_file:
        material = Path(args.from_file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        material = sys.stdin.read()
    else:
        raise SystemExit("organum: 자료가 없습니다 — --from <file> 또는 stdin 파이프.")
    if not material.strip():
        raise SystemExit("organum: 빈 자료로는 distill하지 않습니다.")
    summary = distill_mod.distill(
        state_dir, args.domain, material,
        model=args.model, max_budget_usd=args.max_budget_usd,
        override_streak=args.override_streak,
    )
    print(f"distilled → {summary['path']} (cost {summary['cost_usd']})")
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    state_dir = _require_state()
    _writable_meta(state_dir)
    soma = st.ensure_soma(state_dir, _forid(args))  # 게스트 세포면 자기 soma의 self.md
    summary = reflect.apply(
        soma,
        patterns=args.pattern,
        lessons=args.lesson,
        questions=args.question,
        resolve=args.resolve,
        trigger=args.trigger,
    )
    print(
        f"reflected: +{summary['added']} 항목 · resolve {summary['resolved']} "
        f"(trigger: {summary['trigger']})"
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    state_dir = _require_state()  # 버전 게이트는 migrate 자신이 한다 (_writable_meta 우회)
    result = migrate_mod.migrate(state_dir)
    if result["status"] == "current":
        print(f"이미 최신 포맷 v{result['version']} — 마이그레이션할 것 없음.")
    else:
        print(f"migrated v{result['from']} → v{result['to']} (backup: {result['backup']})")
    return 0


def cmd_checkup(args: argparse.Namespace) -> int:
    state_dir = _require_state()
    findings = checkup_mod.run(state_dir)
    text, has_error = checkup_mod.render(findings)
    print(text)
    try:  # 의례에 편승하는 관측 스윕 — 실패해도 점검을 막지 않는다
        from organum import adapters as _ad
        from organum import observatory as _obs
        n = _obs.record(state_dir, _ad.snapshot(state_dir.parent, window_min=45 * 24 * 60),
                        reason="checkup")
        if n:
            print(f"observatory: +{n} 세션 스냅샷")
    except Exception as e:
        print(f"observatory: 스윕 실패 ({e})", file=sys.stderr)
    return 1 if has_error else 0


def cmd_observatory(args: argparse.Namespace) -> int:
    from organum import adapters as _ad
    from organum import observatory as _obs
    state_dir = _require_state()
    if args.obs_cmd == "sync":
        cells = _ad.snapshot(state_dir.parent, window_min=args.window * 24 * 60)
        for extra in args.also:  # 개명 전 옛 경로 등 — 프로젝트 이사로 갈라진 이력 편입
            cells += _ad.snapshot(Path(extra).expanduser(), window_min=args.window * 24 * 60)
        n = _obs.record(state_dir, cells, reason="sync")
        print(f"observatory: +{n} 세션 스냅샷 (발견 창 {args.window}일, 중복 제외)")
        return 0
    if args.obs_cmd == "report":
        if args.html:
            from organum.htmlreport import observatory_page
            from organum.inspect import ts_age_seconds
            live = [c for c in _ad.snapshot(state_dir.parent, window_min=30.0)
                    if (ts_age_seconds(c.get("last_ts")) or 9e9) <= 1800]
            recs = _obs.load(state_dir, since_days=args.days)
            out = Path(args.html).expanduser()
            out.write_text(observatory_page(live, recs, state_dir.parent.name, args.days),
                           encoding="utf-8")
            print(f"HTML 리포트: {out} (live {len(live)} · 역사 {len(recs)})")
            return 0
        print(_obs.report(state_dir, state_dir.parent, days=args.days))
        return 0
    recs = _obs.load(state_dir, since_days=args.days)
    print(_obs.render_stats(_obs.stats(recs, by=args.by), args.days, by=args.by))
    return 0


def cmd_inspector(args: argparse.Namespace) -> int:
    from organum import inspector as insp
    return insp.main([args.path, "--window", str(args.window)]
                     + (["--json"] if args.json else [])
                     + (["--html", args.html] if args.html else []))


def cmd_backup(args: argparse.Namespace) -> int:
    state_dir = st.find_state_dir(Path.cwd())
    if state_dir is None:
        print("organum: .organum/이 없습니다. 먼저 'organum init'.", file=sys.stderr)
        return 1
    meta = st.load_meta(state_dir)
    st.check_format_version(meta)
    dest = Path(args.to) if args.to else st.default_backup_dir(state_dir.parent)
    archive = st.create_backup(state_dir, dest)
    # 이벤트는 아카이브 생성 뒤에 기록 — 아카이브 자신에는 포함되지 않는다 (§5)
    st.append_event(state_dir, "backup", f"backup → {archive}")
    print(f"backup: {archive}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    archive = Path(args.archive)
    if not archive.is_file():
        print(f"organum: {archive} 파일이 없습니다.", file=sys.stderr)
        return 1
    meta = st.read_archive_meta(archive)
    warning = st.check_format_version(meta)  # 미래 버전이면 여기서 거부 (§5)

    target = Path.cwd() / st.STATE_DIR_NAME
    ts = st.utc_now_iso().replace(":", "").replace("-", "")
    if target.exists():
        if not args.force:
            print(
                f"organum: {target}가 이미 있습니다. 덮어쓰려면 --force "
                "(기존 상태는 삭제되지 않고 .organum.pre-restore-*로 보존됩니다).",
                file=sys.stderr,
            )
            return 1
    tmp = target.with_name(f"{st.STATE_DIR_NAME}.tmp-{ts}")
    st.extract_archive(archive, tmp)
    if target.exists():
        preserved = target.with_name(f"{st.STATE_DIR_NAME}.pre-restore-{ts}")
        target.rename(preserved)
        print(f"기존 상태 보존: {preserved}")
    tmp.rename(target)
    st.append_event(target, "restore", f"restore ← {archive}")
    print(f"restored: {target}")
    if warning:
        print(f"organum: {warning}", file=sys.stderr)
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    from organum import provision as provision_mod
    result = provision_mod.provision(
        Path(args.skill_dir), Path(args.into), trust_override=args.trust
    )
    print(f"provisioned '{result['skill']}' → {result['annex']}")
    print(f"  wired organs: {', '.join(result['wired'])}")
    print(f"  allowed-tools: {result['tools']}")
    for f in result["audit"]:
        print(f"  · audit: {f}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from organum import inspect as inspect_mod

    cwd = Path.cwd()
    state_dir = st.find_state_dir(cwd)  # 선택 — .organum 없어도 transcript vitals는 뜬다
    return inspect_mod.run(
        cwd, state_dir,
        transcript=args.transcript, once=args.once, interval=args.interval,
        all_cells=args.all,
    )


def cmd_live(args: argparse.Namespace) -> int:
    """thin tmux 런처 — 왼쪽 pane=네이티브 작업(claude 등), 오른쪽=organum inspect.

    경계: dumb launch. organum은 tmux 배치 + read-only 인스펙터만 제공한다. 작업은
    네이티브 CLI에서. organum이 워커를 spawn/route 하지 않는다.
    """
    import os
    import shlex
    import shutil as _sh
    import subprocess

    cwd = Path.cwd()
    # 오른쪽 pane은 새 셸이라 PATH에 organum이 없을 수 있다(venv 미활성 등) →
    # 현재 인터프리터 절대경로로 모듈 실행하면 어느 설치 방식이든 견고하다.
    inspect_prog = f"{shlex.quote(sys.executable)} -m organum inspect"
    if getattr(args, "all", False):
        inspect_prog += " --all"  # 오른쪽 pane을 멀티-세포 뷰로

    if _sh.which("tmux") is None:
        print("organum live: tmux가 없습니다. 수동으로도 됩니다 —")
        print("  1) 터미널을 두 창/패널로 나눈다")
        print("  2) 한 쪽에서 네이티브 CLI(claude 등)로 작업")
        print(f"  3) 다른 쪽에서:  {inspect_prog}")
        return 1

    session = args.session
    # tmux 서버는 터미널을 닫아도 백그라운드에 살아남는다(처음 쓰면 헷갈리는 지점).
    # --stop = 명확한 OFF 스위치. --fresh = 죽이고 새로.
    if getattr(args, "stop", False):
        r = subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        print(f"organum live: '{session}' 세션 종료." if r.returncode == 0
              else f"organum live: 실행 중인 '{session}' 세션이 없습니다.")
        return 0
    if getattr(args, "fresh", False):
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    # 같은 이름 세션이 이미 있으면 새로 만들지 않고 그리로 붙는다 (작업 이어가기)
    exists = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    ).returncode == 0
    if not exists:
        left = args.cli or os.environ.get("SHELL", "sh")
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(cwd), left], check=True)
        subprocess.run(["tmux", "split-window", "-h", "-p", "34", "-t", session, "-c", str(cwd), inspect_prog], check=True)
        subprocess.run(["tmux", "select-pane", "-t", f"{session}.0"], check=True)
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    return 0  # execvp 성공 시 도달 안 함


def cmd_web(args: argparse.Namespace) -> int:
    from organum import web as web_mod

    cwd = Path.cwd()
    state_dir = st.find_state_dir(cwd)  # 선택
    return web_mod.serve(cwd, state_dir, port=args.port, host=args.host)


def cmd_mcp(args: argparse.Namespace) -> int:
    from organum import mcp as mcp_mod

    st.require_state_dir(Path.cwd())  # 미초기화면 여기서 명확히 실패
    mcp_mod.serve(Path.cwd(), _forid(args))
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    from organum import relay as relay_mod

    cwd = st.require_state_dir(Path.cwd()).parent
    if args.relay_cmd == "send":
        body = args.body
        if body is None and not sys.stdin.isatty():
            body = sys.stdin.read()
        fn = relay_mod.send(cwd, body or "", frm=_from(args), to=args.to, topic=args.topic or "",
                            thread=args.thread or "", reply_to=args.reply_to or "",
                            escalate=args.escalate)
        if not fn:
            raise SystemExit("organum relay: 빈 본문 — 본문을 인자나 stdin으로.")
        print(fn)
        return 0
    if args.relay_cmd == "inbox":
        msgs = relay_mod.inbox(cwd, _need_forid(args), include_read=args.all)
        if not msgs:
            print("(새 편지 없음)")
            return 0
        for m in msgs:
            head = f"— {m['from']} → {m['to']}" + (f" · {m['topic']}" if m["topic"] else "")
            if m.get("thread"):
                head += f" · thread:{str(m['thread'])[:12]}"
            print(f"{head} · {m['ts']}  [{m['file']}]")
            print(m["body"])
            print()
        return 0
    if args.relay_cmd == "read":
        relay_mod.mark_read(cwd, _need_forid(args), args.file)
        print(f"read: {args.file}")
        return 0
    if args.relay_cmd == "join":
        print(f"joined: {relay_mod.mark_join(cwd, _need_forid(args))}")
        return 0
    if args.relay_cmd == "watch":
        def _emit(m):
            head = f"— {m['from']} → {m['to']}" + (f" · {m['topic']}" if m["topic"] else "")
            if m.get("thread"):
                head += f" · thread:{str(m['thread'])[:12]}"
            print(f"{head} · {m['ts']}  [{m['file']}]")
            print(m["body"])
            print(flush=True)
        print(f"watching · {_need_forid(args)[:8]} · every {args.interval}s · idle {args.idle}s (Ctrl-C 종료)",
              file=sys.stderr)
        try:
            n = relay_mod.watch(cwd, _need_forid(args), _emit, interval=args.interval, idle=args.idle)
            print(f"(idle — {n}통 전달 후 종료)", file=sys.stderr)
        except KeyboardInterrupt:
            print("\n(중단)", file=sys.stderr)
        return 0
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    from organum import roster as roster_mod

    cwd = Path.cwd()
    if args.roster_cmd == "me":
        state_cwd = st.require_state_dir(cwd).parent  # 선언은 초기화된 .organum/ 필요
        open_to = None
        if args.open_to is not None:
            open_to = [s for s in args.open_to.split(",") if s.strip()]
        e = roster_mod.write_presence(
            state_cwd, _need_forid(args), name=args.name, focus=args.focus, open_to=open_to, brain=args.brain)
        out = f"presence: {e['id']}"
        if e.get("name"):
            out += f" · {e['name']}"
        if e.get("focus"):
            out += f" · focus: {e['focus']}"
        if e.get("open_to"):
            out += " · open: " + ",".join(e["open_to"])
        print(out)
        return 0
    # 목록 — 선언 presence + 벤더별 transcript 파생 관찰(Claude·Codex…)을 병합 (어댑터가 계산)
    from organum import adapters as adapters_mod
    from organum import inspect as inspect_mod

    derived = []
    for c in adapters_mod.snapshot(cwd, window_min=args.window):
        age = inspect_mod.ts_age_seconds(c["last_ts"])
        derived.append({
            "id": c["id"], "brain": c["model"], "origin": c["origin"], "vendor": c["vendor"],
            "last_ts": c["last_ts"], "age": age,
            "live": bool(age is not None and age <= args.live_secs),
        })
    cells = roster_mod.merge(roster_mod.read_presence(cwd), derived, live_secs=args.live_secs)
    print(roster_mod.render(cells, cwd.name))
    return 0


def cmd_agora(args: argparse.Namespace) -> int:
    from organum import agora as agora_mod

    cwd = st.require_state_dir(Path.cwd()).parent
    if args.agora_cmd == "post":
        body = args.body
        if body is None and not sys.stdin.isatty():
            body = sys.stdin.read()
        fn = agora_mod.post(cwd, body or "", frm=_from(args), topic=args.topic or "",
                            thread=args.thread or "", reply_to=args.reply_to or "",
                            escalate=args.escalate)
        if not fn:
            raise SystemExit("organum agora: 빈 본문 — 본문을 인자나 stdin으로.")
        print(fn)
        return 0

    def _emit(m):
        head = f"— {m['from']}" + (f" · {m['topic']}" if m["topic"] else "")
        if m.get("thread"):
            head += f" · thread:{str(m['thread'])[:12]}"
        print(f"{head} · {m['ts']}  [{m['file']}]")
        print(m["body"])
        print()

    if args.agora_cmd == "read":
        msgs = agora_mod.read(cwd, _need_forid(args), include_read=args.all)
        if not msgs:
            print("(새 글 없음)")
            return 0
        for m in msgs:
            _emit(m)
        return 0
    if args.agora_cmd == "join":
        print(f"joined: {agora_mod.mark_join(cwd, _need_forid(args))}")
        return 0
    if args.agora_cmd == "watch":
        print(f"watching agora · {_need_forid(args)[:8]} · every {args.interval}s · idle {args.idle}s (Ctrl-C 종료)",
              file=sys.stderr)
        try:
            n = agora_mod.watch(cwd, _need_forid(args), _emit, interval=args.interval, idle=args.idle)
            print(f"(idle — {n}건 후 종료)", file=sys.stderr)
        except KeyboardInterrupt:
            print("\n(중단)", file=sys.stderr)
        return 0
    return 0


def cmd_alarm(args: argparse.Namespace) -> int:
    from organum import alarm as alarm_mod

    state_dir = st.require_state_dir(Path.cwd())
    cwd = state_dir.parent
    if args.alarm_cmd == "sound":
        body = args.body
        if body is None and not sys.stdin.isatty():
            body = sys.stdin.read()
        try:
            fn = alarm_mod.sound(cwd, state_dir, body or "", frm=_from(args), to=args.to,
                                 level=args.level)
        except alarm_mod.AlarmError as e:
            raise SystemExit(f"organum alarm: {e}")
        if not fn:
            raise SystemExit("organum alarm: 빈 본문 — 사유를 인자나 stdin으로.")
        print(fn)
        return 0
    if args.alarm_cmd == "active":
        alarms = alarm_mod.active(cwd, _forid(args))
        if not alarms:
            print("(활성 경보 없음)")
            return 0
        for a in alarms:
            print(f"⚠ [{a['level']}] {a['from']} → {a['to']} · {a['ts']}  [{a['file']}]")
            print(a["body"])
            print()
        return 0
    if args.alarm_cmd == "resolve":
        ok = alarm_mod.resolve(cwd, args.file)
        if not ok:
            raise SystemExit(f"organum alarm: 해제 실패 — '{args.file}' 없음.")
        print(f"resolved: {args.file}")
        return 0
    if args.alarm_cmd == "watch":
        def _emit(m):
            print(f"⚠ [{m.get('topic') or 'notice'}] {m['from']} → {m['to']} · {m['ts']}  [{m['file']}]")
            print(m["body"])
            print(flush=True)
        print(f"watching alarm · {_need_forid(args)[:8]} · every {args.interval}s · idle {args.idle}s (Ctrl-C 종료)",
              file=sys.stderr)
        try:
            n = alarm_mod.watch(cwd, _need_forid(args), _emit, interval=args.interval, idle=args.idle)
            print(f"(idle — {n}건 후 종료)", file=sys.stderr)
        except KeyboardInterrupt:
            print("\n(중단)", file=sys.stderr)
        return 0
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    from organum import session as session_mod

    if args.session_cmd == "start":
        state_dir = _require_state()
        meta = _writable_meta(state_dir)
        soma = st.ensure_soma(state_dir, _forid(args))
        cell = _forid(args) or meta.get("agent", "cell")
        charter = session_mod.resolve_charter(state_dir, args.role)
        try:
            rec = session_mod.start(soma, cell, args.role, args.intent, charter)
        except session_mod.SessionError as e:
            raise SystemExit(f"organum session: {e}")
        print(f"session start · {rec['sid']} · [{rec['role']}] {rec['intent']}")
        if not args.quiet_charter:
            print("\n" + charter.rstrip())
        return 0

    if args.session_cmd == "note":
        state_dir = _require_state()
        _writable_meta(state_dir)
        soma = st.ensure_soma(state_dir, _forid(args))
        try:
            rec = session_mod.note(soma, args.text)
        except session_mod.SessionError as e:
            raise SystemExit(f"organum session: {e}")
        print(f"note +1 · {rec['sid']} · {len(rec['notes'])} beats")
        return 0

    if args.session_cmd == "status":
        state_dir = _require_state()
        soma = st.soma_dir(state_dir, _forid(args))
        s = session_mod.status(soma)
        if s is None:
            print("(열린 세션 없음)")
            return 0
        print(f"session {s['sid']} · [{s['role']}] {s['intent']}")
        print(f"  age {s['age_min']}m · idle {s['idle_min']}m · {s['notes']} beats · since {s['started_at']}")
        return 0

    if args.session_cmd == "end":
        state_dir = _require_state()
        _writable_meta(state_dir)
        soma = st.ensure_soma(state_dir, _forid(args))
        peers = []
        for pj in args.peer_json:
            try:
                peers.append(json.loads(pj))
            except json.JSONDecodeError as e:
                raise SystemExit(f"organum session: --peer-json 파싱 실패 — {e}")
        try:
            rec = session_mod.end(soma, shipped=args.ship, peers=peers)
        except session_mod.SessionError as e:
            raise SystemExit(f"organum session: {e}")
        if args.lesson or args.pattern:  # carry-forward는 reflect 재사용 (별도 조직 아님)
            reflect.apply(soma, patterns=args.pattern, lessons=args.lesson,
                          questions=[], resolve=[], trigger=f"session {rec['sid']}")
        print(f"session end · {rec['sid']} · {rec['duration_min']}m · "
              f"{len(rec['notes'])} beats · {len(rec['shipped'])} shipped")
        for s in rec["shipped"]:
            print(f"  ✓ {s}")
        for pn in rec["peers"]:
            pair = {True: "pair again", False: "no pair", None: "—"}[pn["would_pair_again"]]
            line = f"  ~ {pn['peer']}: +{len(pn['strengths'])} / −{len(pn['frictions'])} · {pair}"
            if pn["role_fit"]:
                line += f" · {pn['role_fit']}"
            print(line)
        return 0

    return 0


def cmd_join(args: argparse.Namespace) -> int:
    """세포 자기-온보딩 한 방: id 자동 · 세션 선언 · relay/agora 가입 · [헌장+goal+inbox] 출력.
    사람 몫은 터미널당 한 문장('너는 <역할>이야 — organum join --role <역할>')으로 줄어든다.
    roster me처럼 자기선언이지 배정 아님 — organum은 모아서 보여줄 뿐 일을 시키지 않는다."""
    import uuid
    from organum import alarm as alarm_mod
    from organum import session as session_mod
    from organum import relay as relay_mod
    from organum import agora as agora_mod

    state_dir = _require_state()
    _writable_meta(state_dir)
    cwd = state_dir.parent  # relay/agora field 루트
    cid = _forid(args) or uuid.uuid4().hex[:8]
    soma = st.ensure_soma(state_dir, cid)
    charter = session_mod.resolve_charter(state_dir, args.role)

    try:
        session_mod.start(soma, cid, args.role, args.intent or f"{args.role} 세션", charter)
        started = True
    except session_mod.SessionError:
        started = False  # 이미 열린 세션 — 이어감

    relay_mod.mark_join(cwd, cid)
    agora_mod.mark_join(cwd, cid)

    print(f"● joined as {cid} · role {args.role}" + ("" if started else " (기존 세션 이어감)"))
    print(f"→ export ORGANUM_CELL={cid}   # 이후 organum 커맨드에서 --for 생략 가능")
    alarms = alarm_mod.active(cwd, cid)
    if alarms:
        print("\n── ⚠ 활성 경보 (규율: pause면 원자 작업만 마치고 정지+ACK) ──")
        for a in alarms:
            print(f"  ⚠ [{a['level']}] {a['from']}: {(a['body'] or '').strip()[:200]}")
    print(f"\n── 역할 헌장 · roles/{args.role} ──")
    print(charter.rstrip())
    print("\n── 오늘의 goal · 최근 agora ──")
    posts = agora_mod.list_all(cwd, limit=3)
    if posts:
        for m in posts[-2:]:
            print(f"  [{m.get('from', '?')}] {(m.get('body') or '').strip()}")
    else:
        print("  (아직 goal 없음 — 사람이 'organum agora post'로 올립니다)")
    print("\n── inbox ──")
    inbox = relay_mod.inbox(cwd, cid)
    if inbox:
        for m in inbox:
            print(f"  ← {m.get('from', '?')}: {(m.get('body') or '').strip()[:200]}")
    else:
        print("  (새 편지 없음)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="organum",
        description="Organism Engineering CLI — 상태와 규율의 도구 (에이전트가 아님).",
    )
    parser.add_argument("--version", action="version", version=f"organum {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help=".organum/ 상태 디렉터리 생성 + 지도 시드")
    p_init.add_argument("--agent", default="agent", help="이 인스턴스를 운용하는 에이전트 이름")
    p_init.set_defaults(func=cmd_init)

    p_ctx = sub.add_parser("context", help="[Self]+[Map]+[WM] 주입 블록을 stdout으로")
    p_ctx.add_argument("--for", dest="for_id", default=None,
                       help="이 세포 id — 공존 게스트면 자기 soma의 [Self]. 생략=owner (§2.3)")
    p_ctx.set_defaults(func=cmd_context)

    p_rem = sub.add_parser("remember", help="기억 저장 — guard 저장 경계 경유 (§7)")
    p_rem.add_argument("content", help="기억할 내용 (레코드당 한 주장)")
    p_rem.add_argument("--type", choices=memory.VALID_TYPES, default="episodic")
    p_rem.add_argument("--tags", default="", help="쉼표 구분. 규약: field:<맥락> · place:<map 경로>")
    p_rem.add_argument("--confidence", choices=memory.VALID_CONFIDENCE, default="tentative")
    p_rem.add_argument("--supersedes", default=None, help="정정할 기존 기억 id (삭제 대신 대체)")
    p_rem.add_argument("--for", dest="for_id", default=None,
                       help="이 세포 id — 공존 게스트면 자기 soma에 저장. 생략=owner (§2.3)")
    p_rem.set_defaults(func=cmd_remember)

    p_rec = sub.add_parser("recall", help="시간창 질의 — 지난 N시간 무슨 일이 있었나")
    p_rec.add_argument("--when", required=True, help="시간창: 30m · 24h · 7d · 2w")
    p_rec.add_argument("--for", dest="for_id", default=None,
                       help="이 세포 id — 공존 게스트면 자기 soma 조회. 생략=owner (§2.3)")
    p_rec.set_defaults(func=cmd_recall)

    p_map = sub.add_parser("map", help="리포 지도 (GIVEN-MAP 시드 + frontier)")
    map_sub = p_map.add_subparsers(dest="map_cmd")
    map_sub.add_parser("frontier", help="미탐험(안 읽은) 파일 목록")
    pm_mark = map_sub.add_parser("mark", help="파일을 read로 마킹 (+blob sha 캡처)")
    pm_mark.add_argument("path")
    pm_mark.add_argument("--note", default=None, help="한 줄 노트 (문단 금지 — 서술은 worldmodel에)")
    map_sub.add_parser("sync", help="재열거 병합: 신규=unvisited, 소실 제거, read 보존")
    p_map.set_defaults(func=cmd_map, map_cmd=None)

    p_dis = sub.add_parser("distill", help="세션 자료 → 세계모델 (form-first 강제, §3.5)")
    p_dis.add_argument("--domain", required=True, help="세계모델 도메인 슬러그 ([a-z0-9-]+)")
    p_dis.add_argument("--from", dest="from_file", default=None, help="자료 파일 (없으면 stdin)")
    p_dis.add_argument("--model", default=None, help="위임할 모델 (기본: 사용자 CLI 기본값)")
    p_dis.add_argument("--max-budget-usd", type=float, default=1.0, help="위임 예산 캡 (콜드캐시 기준 $1+ 권장)")
    p_dis.add_argument("--override-streak", action="store_true", help="guard streak 활성 시에도 강제 위임")
    p_dis.set_defaults(func=cmd_distill)

    p_ref = sub.add_parser("reflect", help="세션 회고 → self.md carry-forward (§3.2)")
    p_ref.add_argument("--pattern", action="append", default=[], help="Patterns 섹션에 항목 추가 (반복 가능, 끝에 (evidence: ...))")
    p_ref.add_argument("--lesson", action="append", default=[], help="Lessons 섹션에 항목 추가 (반복 가능)")
    p_ref.add_argument("--question", action="append", default=[], help="Open questions에 항목 추가 (반복 가능)")
    p_ref.add_argument("--resolve", action="append", default=[], help="매치되는 Open question 제거 (부분 문자열)")
    p_ref.add_argument("--trigger", default=None, help="이 회고의 계기 (Last reflection 줄에 기록)")
    p_ref.add_argument("--for", dest="for_id", default=None,
                       help="이 세포 id — 공존 게스트면 자기 soma의 self.md. 생략=owner (§2.3)")
    p_ref.set_defaults(func=cmd_reflect)

    p_mig = sub.add_parser("migrate", help="포맷 버전 마이그레이션 (§10 — 자동 backup 후 변환)")
    p_mig.set_defaults(func=cmd_migrate)

    p_chk = sub.add_parser("checkup", help="상태 건강 점검 (streak·무결성·staleness·백업)")
    p_chk.set_defaults(func=cmd_checkup)

    p_bak = sub.add_parser("backup", help="상태 스냅샷 tar.gz 생성 (tmp/ 제외, day 1 기능)")
    p_bak.add_argument("--to", default=None, help="목적지 디렉터리 (기본: ~/.organum/backups/...)")
    p_bak.set_defaults(func=cmd_backup)

    p_res = sub.add_parser("restore", help="스냅샷에서 .organum/ 복원 (기존 상태는 보존)")
    p_res.add_argument("archive", help="organum backup이 만든 tar.gz 경로")
    p_res.add_argument("--force", action="store_true", help="기존 .organum/이 있어도 진행")
    p_res.set_defaults(func=cmd_restore)

    p_prov = sub.add_parser("provision", help="변환된 Agent Skill에 조직 배선 (신뢰 출처 감사)")
    p_prov.add_argument("skill_dir", help="SKILL.md를 포함한 skill 디렉터리")
    p_prov.add_argument("--into", default=".", help="provision 대상 작업 디렉터리 (기본: cwd)")
    p_prov.add_argument("--trust", action="store_true", help="신뢰 출처 미선언 skill도 provision (감사는 수행)")
    p_prov.set_defaults(func=cmd_provision)

    p_ins = sub.add_parser("inspect", help="라이브 유기체 vitals (세션 transcript tail — read-only 관찰)")
    p_ins.add_argument("--once", action="store_true", help="한 번만 렌더하고 종료 (파이프/테스트용)")
    p_ins.add_argument("--interval", type=float, default=1.0, help="갱신 주기(초, 기본 1.0)")
    p_ins.add_argument("--transcript", default=None, help="transcript.jsonl 직접 지정 (자동탐지 실패 시)")
    p_ins.add_argument("--all", action="store_true", help="이 현장의 활성 세션(세포) 전부 수렴 — 멀티-에이전트 뷰")
    p_ins.set_defaults(func=cmd_inspect)

    p_liv = sub.add_parser("live", help="tmux 런처: 네이티브 CLI 작업 + organum inspect 나란히")
    p_liv.add_argument("--cli", default=None, help="왼쪽 pane에서 자동 실행할 CLI (기본: 셸 — 직접 claude 실행)")
    p_liv.add_argument("--session", default="organum", help="tmux 세션 이름 (기본: organum)")
    p_liv.add_argument("--fresh", action="store_true", help="기존 세션을 죽이고 새로 시작 (옛 프로세스 재사용 방지)")
    p_liv.add_argument("--all", action="store_true", help="오른쪽 inspect pane을 멀티-세포(--all) 뷰로")
    p_liv.add_argument("--stop", action="store_true", help="실행 중인 세션 종료 (tmux 서버는 창 닫아도 남는다 — 이게 OFF 스위치)")
    p_liv.set_defaults(func=cmd_live)

    p_web = sub.add_parser("web", help="관제탑 — localhost 웹으로 현장의 모든 세포 수렴 (read-only · tmux 불요 · 크로스-플랫폼)")
    p_web.add_argument("--port", type=int, default=7332, help="포트 (기본 7332, 사용 중이면 다음 것 시도)")
    p_web.add_argument("--host", default="127.0.0.1", help="바인드 호스트 (기본 127.0.0.1 = localhost)")
    p_web.set_defaults(func=cmd_web)

    p_insp2 = sub.add_parser("inspector", help="사후 계측 — 임의 폴더의 에이전트 세션 소급 집계 (read-only·init 불요; 라이브 tail은 'inspect')")
    p_insp2.add_argument("path", nargs="?", default=".", help="프로젝트 폴더 (기본: 현재 폴더)")
    p_insp2.add_argument("--window", type=float, default=45, help="발견 창(일, 기본 45)")
    p_insp2.add_argument("--json", action="store_true", help="기계용 JSON 출력")
    p_insp2.add_argument("--html", metavar="FILE", help="자립형 HTML 리포트로 저장")
    p_insp2.set_defaults(func=cmd_inspector)

    p_obs = sub.add_parser("observatory", help="관측 영속화 — 세션 소비 스냅샷 축적(월 샤드)·통계 (transcript ~30일 시한부 대비)")
    obs_sub = p_obs.add_subparsers(dest="obs_cmd", required=True)
    po_sync = obs_sub.add_parser("sync", help="발견 가능한 세션 전부 스윕 → 신규/전진분만 기록 (멱등)")
    po_sync.add_argument("--window", type=float, default=45, help="발견 창(일, 기본 45 — transcript 청소 주기보다 넓게)")
    po_sync.add_argument("--also", action="append", default=[],
                         help="추가 프로젝트 경로의 세션도 편입 (개명/이사 전 옛 경로 — 반복 가능)")
    po_stats = obs_sub.add_parser("stats", help="축적된 스냅샷 집계 — 세션·토큰·비용 근사·모델 믹스")
    po_stats.add_argument("--days", type=float, default=30, help="집계 기간(일, 기본 30)")
    po_stats.add_argument("--by", choices=["model", "role", "origin", "vendor"], default=None,
                          help="그룹 축 (모델/역할/기원/벤더)")
    po_rep = obs_sub.add_parser("report", help="작업 모니터 리포트 — 지금(live)/오늘/역사를 분리된 밴드로")
    po_rep.add_argument("--days", type=float, default=30, help="역사 창(일, 기본 30)")
    po_rep.add_argument("--html", metavar="FILE", help="자립형 HTML 리포트로 저장 (지금/역사 밴드)")
    p_obs.set_defaults(func=cmd_observatory)

    p_mcp = sub.add_parser("mcp", help="MCP(stdio) 서버 — 조율(relay/agora/roster)을 MCP 툴로 노출 (한 세포). MCP 에이전트가 native로 조율")
    p_mcp.add_argument("--for", dest="for_id", default=None, help="이 서버가 대변하는 세포 id (생략 시 ORGANUM_CELL/owner)")
    p_mcp.set_defaults(func=cmd_mcp)

    p_rel = sub.add_parser("relay", help="폴더 우체통 — 세포 간 비동기 편지 (send/inbox/read)")
    rel_sub = p_rel.add_subparsers(dest="relay_cmd", required=True)
    rs = rel_sub.add_parser("send", help="편지 드롭 (.organum/relay/)")
    rs.add_argument("body", nargs="?", default=None, help="본문 (없으면 stdin)")
    rs.add_argument("--to", default="all", help="수신 (all · 세포 id 콤마 · 역할)")
    rs.add_argument("--from", dest="frm", default=None, help="발신 이름 (생략 시 ORGANUM_CELL → 'cell')")
    rs.add_argument("--topic", default="", help="주제 (선택)")
    rs.add_argument("--thread", default="", help="스레드 id (대화 그룹, 선택)")
    rs.add_argument("--reply-to", dest="reply_to", default="", help="답장 대상 편지 파일명 (스레드 자동 상속)")
    rs.add_argument("--escalate", action="store_true",
                    help="human 개입 요청 — 관제탑 에스컬레이션 패널에 뜬다 (처리=human의 보관)")
    ri = rel_sub.add_parser("inbox", help="나에게 온 안 읽은 편지 (to=me/all, 내 편지 제외)")
    ri.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    ri.add_argument("--all", action="store_true", help="읽은 것도 포함")
    rr = rel_sub.add_parser("read", help="편지 읽음 표시 (재처리 방지)")
    rr.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    rr.add_argument("file", help="편지 파일명")
    rj = rel_sub.add_parser("join", help="세포 가입 — 이후 편지만 inbox에 (세션 시작 때 1회, 옛 broadcast 무시)")
    rj.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    rw = rel_sub.add_parser("watch", help="무데몬 저지연 폴러 — 새 편지 오는 대로 (idle 자멸, Ctrl-C 종료)")
    rw.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    rw.add_argument("--interval", type=float, default=3.0, help="폴 간격(초, 기본 3)")
    rw.add_argument("--idle", type=float, default=600.0, help="이 시간 새 편지 없으면 자멸(초, 기본 600)")
    p_rel.set_defaults(func=cmd_relay)

    p_ros = sub.add_parser("roster", help="현장 세포 presence — 누가 있나·무엇을 하나·말 걸 수 있나 (서술적, 배정 아님)")
    p_ros.add_argument("--window", type=float, default=30.0, help="파생 관찰 window(분) — 이 안에 활동한 세포")
    p_ros.add_argument("--live-secs", dest="live_secs", type=float, default=90.0, help="live 판정 임계(초, 내용-timestamp)")
    ros_sub = p_ros.add_subparsers(dest="roster_cmd")
    rmme = ros_sub.add_parser("me", help="내 presence 선언/갱신 (name·focus·open_to)")
    rmme.add_argument("--for", dest="for_id", default=None, help="내 세포 id (세션 id 앞 8자 · 생략 시 ORGANUM_CELL)")
    rmme.add_argument("--name", help="자칭 역할/이름 (예: physis-dev)")
    rmme.add_argument("--focus", help="지금 하는 일")
    rmme.add_argument("--open-to", dest="open_to", help="말 걸어도 되는 것 (쉼표: questions,pairing)")
    rmme.add_argument("--brain", help="모델 (선택 — 보통 transcript에서 자동)")
    p_ros.set_defaults(func=cmd_roster, roster_cmd=None)

    p_ago = sub.add_parser("agora", help="토론장 — 개방 심의 필드 (모두 게시·모두 읽음, 주소지정 없음)")
    ago_sub = p_ago.add_subparsers(dest="agora_cmd", required=True)
    ap = ago_sub.add_parser("post", help="토론장에 게시 (.organum/agora/)")
    ap.add_argument("body", nargs="?", default=None, help="본문 (없으면 stdin)")
    ap.add_argument("--from", dest="frm", default=None, help="발신 이름 (생략 시 ORGANUM_CELL → 'cell')")
    ap.add_argument("--topic", default="", help="주제 (선택)")
    ap.add_argument("--thread", default="", help="스레드 id (대화 그룹, 선택)")
    ap.add_argument("--reply-to", dest="reply_to", default="", help="답장 대상 글 파일명 (스레드 자동 상속)")
    ap.add_argument("--escalate", action="store_true",
                    help="human 개입 요청 — 관제탑 에스컬레이션 패널에 뜬다 (처리=human의 보관)")
    ar = ago_sub.add_parser("read", help="토론장의 안 읽은 새 글 (내 것 제외, 가입 이후)")
    ar.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    ar.add_argument("--all", action="store_true", help="읽은 것도 포함")
    aj = ago_sub.add_parser("join", help="토론장 가입 — 이후 글만 (세션 시작 때 1회)")
    aj.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    aw = ago_sub.add_parser("watch", help="무데몬 저지연 폴러 — 새 글 오는 대로 (idle 자멸)")
    aw.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    aw.add_argument("--interval", type=float, default=3.0, help="폴 간격(초, 기본 3)")
    aw.add_argument("--idle", type=float, default=600.0, help="이 시간 새 글 없으면 자멸(초, 기본 600)")
    p_ago.set_defaults(func=cmd_agora)

    p_alm = sub.add_parser("alarm", help="경보 필드 — 발동은 human/chief만, 모두가 읽음 (pause=정지 권고, 강제 아님)")
    alm_sub = p_alm.add_subparsers(dest="alarm_cmd", required=True)
    als = alm_sub.add_parser("sound", help="경보 발동 (.organum/alarm/) — human 또는 chief(열린 세션)만")
    als.add_argument("body", nargs="?", default=None, help="사유 (없으면 stdin)")
    als.add_argument("--to", default="all", help="대상 (all · 세포 id 콤마)")
    als.add_argument("--from", dest="frm", default=None, help="발동자 (human 또는 chief 세포 id — 생략 시 ORGANUM_CELL)")
    als.add_argument("--level", choices=["notice", "pause"], default="notice",
                     help="notice=주의 · pause=정지 권고 (규율: 원자 작업만 마치고 정지+ACK)")
    ala = alm_sub.add_parser("active", help="활성(미해제) 경보 — --for를 주면 그 세포에게 유효한 것만")
    ala.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL → 전체)")
    alr = alm_sub.add_parser("resolve", help="경보 해제 — 소프트 보관 (가역)")
    alr.add_argument("file", help="경보 파일명")
    alw = alm_sub.add_parser("watch", help="무데몬 저지연 폴러 — 나에게 유효한 새 경보 오는 대로 (idle 자멸)")
    alw.add_argument("--for", dest="for_id", default=None, help="내 세포 id (생략 시 ORGANUM_CELL)")
    alw.add_argument("--interval", type=float, default=3.0, help="폴 간격(초, 기본 3)")
    alw.add_argument("--idle", type=float, default=600.0, help="이 시간 새 경보 없으면 자멸(초, 기본 600)")
    p_alm.set_defaults(func=cmd_alarm)

    p_ses = sub.add_parser("session", help="세션 라이프사이클 — 의도 선언·진행 기록·회고 (state/discipline, 지휘 아님)")
    ses_sub = p_ses.add_subparsers(dest="session_cmd", required=True)
    pss = ses_sub.add_parser("start", help="세션 선언 — 역할·의도 (roster me처럼 자기선언, 배정 아님)")
    pss.add_argument("--role", required=True, help="이 세션의 역할 (engine·reviewer·atelier·scribe·facilitator·chief 또는 커스텀)")
    pss.add_argument("--intent", required=True, help="왜 이 세션인지 한 줄")
    pss.add_argument("--for", dest="for_id", default=None, help="이 세포 id — 게스트면 자기 soma (생략=owner)")
    pss.add_argument("--quiet-charter", dest="quiet_charter", action="store_true", help="시작 시 역할 헌장 출력 생략")
    psn = ses_sub.add_parser("note", help="진행 비트 기록 (자기 규율 체크포인트)")
    psn.add_argument("text", help="비트 내용")
    psn.add_argument("--for", dest="for_id", default=None, help="이 세포 id")
    pst = ses_sub.add_parser("status", help="현재 세션 — 역할·의도·경과·idle·비트 (read-only pull)")
    pst.add_argument("--for", dest="for_id", default=None, help="이 세포 id")
    pse = ses_sub.add_parser("end", help="세션 닫기 — 출하물·피어 저널·carry-forward")
    pse.add_argument("--ship", action="append", default=[], help="이 세션에 출하한 것 (반복 가능)")
    pse.add_argument("--peer-json", dest="peer_json", action="append", default=[],
                     help='피어 노트 JSON (반복): {"peer","strengths":[],"frictions":[],"would_pair_again","role_fit"'
                          '[,"direction":"peer|upward|downward"]} — upward=셀→chief 상향, downward=chief whole-view')
    pse.add_argument("--lesson", action="append", default=[], help="carry-forward Lessons (reflect 재사용)")
    pse.add_argument("--pattern", action="append", default=[], help="carry-forward Patterns (reflect 재사용)")
    pse.add_argument("--for", dest="for_id", default=None, help="이 세포 id")
    p_ses.set_defaults(func=cmd_session)

    p_join = sub.add_parser("join", help="세포 자기-온보딩 한 방 — id 자동·세션 선언·relay/agora 가입·[헌장+goal+inbox] (터미널당 한 줄)")
    p_join.add_argument("--role", required=True, help="이 세포의 역할 — roles/<역할>.md 헌장을 받는다")
    p_join.add_argument("--intent", default=None, help="세션 의도 (생략 시 '<역할> 세션')")
    p_join.add_argument("--for", dest="for_id", default=None, help="세포 id 고정 (생략 시 ORGANUM_CELL 또는 자동생성)")
    p_join.set_defaults(func=cmd_join)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
