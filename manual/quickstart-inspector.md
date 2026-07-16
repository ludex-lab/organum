# organum-inspector — 5분 퀵스타트

*English version: [quickstart-inspector.en.md](quickstart-inspector.en.md)*

같은 과제를 두 에이전트에게 시켰는데, 누가 더 빨랐고 무엇을 더 썼을까?
어제 끝낸 작업이 실제로 토큰을 얼마나 먹었을까? organum-inspector는 그 답을
**사후에** 알려줘요. 에이전트 CLI들이 이미 디스크에 남긴 세션 기록을 읽어서
세션별 소요시간·토큰·툴·파일을 표로 냅니다. 설정할 것도, 프로젝트에 쓰는
것도 없어요.

> pre-1.0(베타)입니다. 포맷이 아직 움직여요.

## 설치

```bash
pip install organum        # 또는: pipx install organum
```

## 1분 컷: 사후 계측

재보고 싶은 프로젝트 폴더를 가리키기만 하면 돼요.

```bash
cd ~/my-project
organum-inspector .
```

```
━ organum inspector · my-project · 창 45일 · 2 세션
  vendor    model         시작            소요       in     out   cache tools files
  codex     gpt-5.6-sol   07-15 10:14    3.4h    34.2M   64.3K   32.3M   430     8
  grok      grok-4.5      07-15 12:10    17.8m    116K       —       —   179    37
```

**계측 대상 터미널에는 아무것도 할 필요가 없어요.** 에이전트 CLI가 어차피
자기 홈 디렉터리에 세션 기록을 남기고, inspector는 그걸 읽기만 해요. `init`도
필요 없고, 지난주에 끝낸 작업에도 그대로 통합니다 — 그게 "사후 계측"이에요.

Claude Code만 되는 것도 아니에요 — Codex, Gemini(Antigravity), Grok, OpenCode
세션이 같은 표에 같은 줄로 정규화돼요.

토큰 자리에 숫자 대신 `—`가 보이면 그건 0이 아니라 "그 벤더가 디스크에 안
남겨서 잴 수 없음"이라는 뜻이에요. 벤더마다 토큰 계수 의미가 달라서, 교차
비교의 안전한 축은 **시간·툴·파일**입니다.

## 브라우저로 보고 공유하기: `--html`

터미널 표 대신 브라우저로 열거나 누군가에게 넘기고 싶으면:

```bash
organum-inspector . --html report.html
```

서버 없이 열리는 자립형 HTML 한 장이 떨어져요 — 타임라인(벤더색 막대),
세션 표, 벤더 비교 바까지. 파일이니까 팀 채널에 던지거나 기록으로 보관하면
됩니다. 기계용으로는 `--json`이 있고, 바로 분석 파이프에 넣을 수 있어요.

## 실제 사례

Codex와 Grok에게 완전히 같은 디자인 과제를 맡겼더니 Grok이 10배 빨랐어요.
그런데 품질은? 셋(발주자·승자·패자)이 교차 평가한 결과 만장일치로 Codex의
승리였고 — 비용은 inspector가, 품질은 에이전트들이 재니 선택이 취향이 아니라
데이터가 됐습니다. 전체 이야기: [case-study-inspector-duel.md](case-study-inspector-duel.md).

## 더 나아가기 — 라이브·역사 (베타)

inspector는 "끝난 작업을 소급 계측"이 전부지만, 같은 `pip install organum`엔
관측 스위트의 다른 조각들이 함께 들어 있어요. 아직 다듬는 중인 베타지만 다
동작합니다:

- **`organum web`** — 라이브 관제탑. 지금 돌고 있는 세션을 브라우저 한곳에
  카드로 모아 실시간으로(모델·토큰·계보) 봅니다. 아무도 안 보면 2시간 뒤
  스스로 종료돼요(`--idle-timeout 0`으로 끄기).
- **`organum observatory`** — 역사 축적. 벤더가 세션 기록을 몇 주 만에
  지워버리기 전에 스냅샷을 쌓아, 30일 너머의 추세·모델 믹스·비용을 봅니다.
  `organum init` 한 번 후 `observatory sync` / `observatory report [--html]`.

이 둘은 별도 문서에서 자세히 다뤄요: [quickstart-observe.md](quickstart-observe.md).

## 경계, 한 번만 분명히

organum은 세션을 시작하지도, 멈추지도, 라우팅하지도 않아요. 읽기 전용 계측이
전부라서 어느 워크플로에 붙여도 아무것도 깨지지 않습니다. inspector는 대상
폴더에 아무것도 쓰지 않고, observatory가 쌓는 데이터도 전부 그 프로젝트의
`.organum/` 로컬이며 기본으로 git에서 제외돼요.

## 자주 묻는 것

**Q. 지금 일하고 있는 이 프로젝트도 잴 수 있나요?**
네. 그 폴더에서 `organum-inspector .` 하면 방금까지의 세션이 바로 표에 떠요.

**Q. 서브에이전트 토큰도 합산되나요?**
네. 서브에이전트는 별도 줄로 뜨고 합계에 포함돼요. 부모 세션만 보면 실제
소비를 한참 놓치는데, 그걸 메우는 게 이 도구의 존재 이유 중 하나예요.

**Q. 예전 세션이 안 보여요.**
inspector는 발견 창(기본 45일) 안의 기록을 읽어요(`--window`로 조절). 단
에이전트 CLI가 오래된 transcript를 지우기 전이어야 해요 — 장기 보존은
observatory(`sync`) 몫입니다.

**Q. 폴더 이름을 바꾸거나 옮겼어요.**
옛 경로의 세션은 자동으론 못 찾아요(기록이 경로 기준). observatory에선
`organum observatory sync --also ~/old-path/project`로 한 번에 편입합니다.
