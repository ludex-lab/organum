# organum bbs 퀵스타트 — 게시판을 읽고 쓰는 가장 짧은 길 (0.6.0)

`organum bbs`는 서명 봉투 위의 게시판·전화번호부입니다. 서버는 dumb한 우체통(hub drop)
그대로이고, **누가 썼는가·순서·귀속은 전부 서명이 집니다.** 0.5.0이 "이벤트 배열 →
상태"의 순수 투영이었다면, 0.6.0은 그 앞뒤를 붙였습니다 — 드롭에서 **수거·검증·정렬**해
읽고, 서명해 **게시**합니다. 참여자에게 새 설치는 없습니다(`pip install organum`).

계약 원문: [게시판](bbs-group-contract-v0.1.md) · [전화번호부](profile-directory-contract-v0.1.md).
hub 쪽(키·hub 디렉터리·드롭)은 [hub 퀵스타트](quickstart-hub.md)가 먼저입니다.

## 준비물 셋

- **hub 디렉터리**: `organum-hub init`으로 만든 자기 원장(`hub/`). 남의 글을 검증하려면
  그 서명자의 키가 여기 등록돼 있어야 합니다(`register-key` / `introduce-signer`).
- **seed**: 자기 서명 키(`organum-hub keygen`). 0600, 원장 밖.
- **드롭 접속 정보**: base URL과 토큰 파일. 시설 운용자에게 받습니다.

## 게시판 목록 — 서버에게 묻는다

```bash
organum-bbs boards --url https://drop.example --token-file token.txt
```

서버의 채널 트리(`GET /v0/channels`)가 **존재하는 문의 권위**입니다. 종류는 각 문의
첫 페이지 내용으로 **추정**합니다(`inferred: true` — 서명 검증 전): 첫 봉투가 JSON
`board.*`면 게시판, `profile.*`면 전화번호부, 둘 다면 `ambiguous`, 계약 신호가 없으면
`unknown`. **unknown은 "게시판이 아니다"가 아닙니다** — 초기 번호 gap·후발 참여·미수거가
있을 수 있어 확정하지 않습니다. 무엇을 수거할지(구독)는 여러분의 선택입니다.

## 읽기 — pull과 read는 다른 동사

```bash
# 1) 수거: 문 전부, 회차 워밍 1회. 로컬 트리는 append-only(덮어쓰지 않음)
organum-bbs pull bbs-plaza --url https://drop.example --token-file token.txt --tree ~/bbs-tree

# 2) 읽기: 오프라인·결정적. 서명·digest·모양 검증 → (at, lab, n) 정렬 → 투영
organum-bbs read bbs-plaza --tree ~/bbs-tree --hub hub
```

- `pull`은 `~/bbs-tree/bbs-plaza/from-<lab>/NNN-*`에 봉투·서명·본문을 내려쓰고,
  회차 상태를 `~/bbs-tree/bbs-plaza/.round.json`에 남깁니다 — 예정 문·성공/실패/미시도·
  정상 응답 페이지 수·**마지막 완결 번호**·유효 timeout. 첫 문을 부르기 전에 `running`으로
  게시하고 문마다 갱신하니 중단돼도 계획이 남습니다. 중간에 끊긴 quad는 완결로 세지 않고
  다음 pull이 그 앞에서 재개해 빈자리(빠진 서명·본문 파일 포함)를 채웁니다. 남아 있는 파일은
  덮어쓰지 않고, 서버 bytes와 다르면 그 문은 실패로 남깁니다. 미완성 quad가 남은 회차는
  성공이 아닙니다(exit 1).
- `read`는 네트워크를 만지지 않고 원장을 갱신하지 않습니다. 같은 입력이면 같은 bytes.
  출력의 `posts`에는 본문(`text`)·`author`·`reply_to`·`at`과 **출처**(`provenance`:
  문·번호·event_id·검증된 서명자)와 정렬 키가 실립니다. `threads`·`members`·`notices`도
  0.5.0 `board`와 같은 모양입니다.
- **거부는 지우지 않습니다.** 서명·digest·모양이 틀린 봉투는 `transport_problems`에
  좌표와 사유로 남고, 계약 위반(비멤버 게시·다른 게시판 좌표·폐쇄 뒤 게시)은
  `rejected`에 남습니다. `rejected_count`·`transport_problem_count`로 "안 보이는 것"이
  "없는 것"으로 읽히지 않게 하세요.
- **완결성을 셋으로 말합니다**(`completeness`): `complete`(닫힌 회차, 문제 0) ·
  `partial`(최근 회차에 실패한 문·미시도·미완성 quad·닫히지 않은 회차 — `round_problems`에
  좌표와 사유) · `unknown`(회차 기록이 없는 트리 — 남의 수거기가 만든 미러 등, 부분인지
  전체인지 이 트리만으로는 말할 수 없음). **전송 문제가 있거나 partial이면 exit 1**입니다 —
  부분 갱신이 수거기 안에서 조용히 지나가지 않게. 확보된 글은 그대로 보이고, 보고만 받고
  진행하려면 `--allow-problems`. unknown은 표시만 하고 exit 0입니다.
- 구조가 틀린 이벤트(예: `author: {}`, `member: "문자열"`)는 서명이 유효해도 그 봉투만
  `transport_problems`로 격리되고 나머지 글은 계속 보입니다. 같은 검사가 `post`에서는
  **서명 전에** 돌아 자기 원장에 남지 않습니다.
- 표시 필터: `--since AT`(at > AT), `--after lab:x:n`(그 봉투 **뒤**). 둘 다 **상태는
  전체 이력으로 계산한 뒤** 표시만 좁힙니다 — 가입·폐쇄·스레드 부모를 잃지 않습니다.
- 전화번호부: `organum-bbs read directory --tree ~/bbs-tree --hub hub --as directory --compiled-at 2026-09-12T00:00:00Z`.
  `compiled_at`은 여러분이 줍니다(read는 시각을 지어내지 않습니다).

이미 자기 수거기가 같은 모양(`<channel>/from-<lab>/NNN-*`)으로 미러를 두고 있다면
`pull`을 건너뛰고 `read --tree <미러>`만 붙이면 됩니다.

### 순서에 대해 정직하게

표시 순서는 `(at, lab, n)`입니다. `at`은 봉투 안 문자열을 그대로 비교합니다(RFC3339 Z
권장). 한 문 안에서 `at`이 역행하면 `n` 순서가 뒤집힙니다 — 물리적 append-only(문 안
번호), 재생 순서(이 정렬), 인과 순서는 서로 다른 보장입니다. `at`이 없는 이벤트는 모양
실패로 배제됩니다.

## 쓰기 — 서명 → 자기 원장 → outbox → push

```bash
cat > post.json <<'EOF'
{"kind": "board.post", "board": "bbs-plaza", "post_id": "plaza-010",
 "author": {"lab": "lab:me", "id": "Cody"}, "reply_to": "plaza-004",
 "at": "2026-09-12T01:00:00Z", "text": "…"}
EOF
organum-bbs post bbs-plaza --event post.json \
  --hub hub --key me.seed --signer lab:me --key-id k1 --epoch 1 \
  --url https://drop.example --token-file token.txt \
  --outbox ~/.organum/hub-home --to-lab lab:host
```

- 순서가 중요합니다: **구조·계약 검증(서명 전)** → 봉투 빌드 → 서명·자기 원장 admit →
  `--outbox`의 `out-<channel>/NNN-*`에 quad 확정 → 그제서야 push. 네트워크가 죽어도
  quad는 남습니다. 원장 읽기부터 quad 배정까지는 hub 디렉터리의 파일 락 아래 한 임계구역
  이라, 같은 이벤트를 동시에 두 번 올려도 원장 한 행·quad 하나로 수렴합니다.
- 응답 유실·timeout·5xx면 `status: unknown`과 재시도 좌표를 돌려줍니다. 재시도는
  **같은 번호·같은 bytes**로:
  `organum-bbs post bbs-plaza --outbox ~/.organum/hub-home --retry NNN --url … --token-file …`
  서버 dedup이 멱등이라 언제나 안전하고, 새 message를 만들지 않으니 중복 발화가 없습니다.
- `409`(같은 번호, 다른 bytes)는 `conflict`로 돌려주고 **자동 우회하지 않습니다.**
  outbox는 **랩당 하나**로 두세요 — 문 번호는 서버와 outbox가 함께 세는 것이라 두 벌이면
  거기서 막힙니다.
- **저장 성공 ≠ 게시판 수용.** 멤버십·좌표·폐쇄는 서버가 아니라 읽는 쪽의 투영이 판정
  합니다. 비멤버로 올린 글은 저장되지만 모두의 `read`에서 `rejected`로 보입니다.

## 등급에 대해

기본 제품 경로는 **claimed**입니다 — "이 랩의 키가 이 글에 서명했다"까지만 말하고,
개인 발화의 verified 등급은 실험 경계(발화 키)에서 opt-in으로만 열립니다. 서명이 참인
것과 그 키가 당시 authority-valid였는가(회전·폐기 이력)는 다른 질문이라 `read`가 둘을
나란히 둡니다.
