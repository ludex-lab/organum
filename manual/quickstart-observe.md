# 내 에이전트 지켜보기 — 5분 퀵스타트

Claude Code로 일하다 보면 궁금해지는 순간이 와요. 지금 토큰을 얼마나 쓰고 있지?
방금 서브에이전트가 몇 개나 떴지? 그건 무슨 모델로 돌았지? organum은 그 답을
보여주는 관측 도구예요. 에이전트를 조종하거나 바꾸는 게 아니라, 이미 디스크에
남는 세션 기록을 읽어서 한 화면으로 모아줄 뿐이에요.

> pre-1.0입니다. 포맷이 아직 움직여요.

## 설치

```bash
git clone https://github.com/JihoonJeong/organum && cd organum
python3 -m venv .venv && .venv/bin/pip install -e .
# 아무 데서나 부르고 싶으면: alias organum=<클론 경로>/.venv/bin/organum
```

## 1분 컷: 관제탑 띄우기

관측하고 싶은 프로젝트 폴더에서 한 줄이면 돼요.

```bash
cd ~/my-project
organum web        # → http://localhost:7332
```

브라우저를 열면 그 프로젝트에서 돌고 있는 세션이 카드로 떠요. 카드마다 모델,
토큰(in/out/cache), 툴 사용 분포, 마지막 활동 시각이 보이고, 세션이 서브에이전트를
띄우면 `subagent ← 부모id` 칩이 붙은 카드가 따로 생겨요. 본 세션은 Fable인데
탐색 서브에이전트는 Opus로 도는 것 같은 모델 믹스도 여기서 처음 눈에 들어와요.

**관측 대상 터미널에는 아무것도 할 필요가 없어요.** Claude Code가 어차피
`~/.claude/projects/`에 세션 기록을 남기고, organum은 그걸 읽기만 해요. 지금
일하는 중인 터미널이든, organum을 들어본 적 없는 프로젝트든 그대로 됩니다.
다른 프로젝트를 하나 더 보고 싶으면 그 폴더에서 `organum web --port 7333`처럼
포트만 바꿔 하나 더 띄우세요.

Claude Code만 되는 것도 아니에요 — Codex, Gemini(Antigravity), Grok, OpenCode
세션도 같은 화면에 같은 카드로 수렴돼요.

## 통계 쌓기: 30일 너머를 보려면

세션 기록엔 함정이 하나 있어요. Claude Code가 오래된 transcript를 한 달쯤 지나면
지워버려요. 그래서 "지난 분기에 이 프로젝트가 토큰을 얼마나 썼나" 같은 질문은
기록이 사라지기 전에 스냅샷을 남겨둬야 답할 수 있어요. 그게 observatory예요.

```bash
cd ~/my-project
organum init                 # .organum/ 상태 폴더 생성 (최초 1회)
organum observatory sync     # 지금 발견되는 세션 전부 스냅샷
organum observatory stats --by model
```

```
observatory — 최근 30일 · 16 세션 (터미널 11 · 서브에이전트 5)
  토큰: in 918.4K · out 943.6K · cache 103.1M
  --by model:
    claude-fable-5               10세션 · in 1.9K · out 881.1K · cache 100.6M
    claude-haiku-4-5-20251001     2세션 · in 566 · out 25.5K · cache 2.1M
    ...
```

한 번 `init` 해두면 이후는 거의 자동이에요. 관제탑이 떠 있는 동안 알아서
기록하고, `organum checkup`(상태 점검 의례)을 돌릴 때마다 스윕해요. 가끔
`sync`를 직접 불러도 되고요 — 같은 세션을 여러 번 스윕해도 중복은 안 쌓여요.
`--by role`, `--by origin`, `--by vendor`로 축을 바꿔 볼 수 있어요.

"지금 vs 역사"를 한 화면으로 보고 싶으면 리포트가 있어요:

```bash
organum observatory report
```

살아있는 세션(실시간), 오늘, 역사(일별 추이·모델 믹스·대형 세션 순위)가
분리된 밴드로 나와요. 프로젝트 소비는 보통 며칠에 한 번 오는 대형 세션이
지배해서, 이 분리가 없으면 현재 화면만 보고 규모를 한참 얕보게 돼요.

프로젝트 폴더 이름을 바꿨거나 옮긴 적이 있다면 옛 경로의 세션은 자동으론 못
찾아요(세션 기록이 경로 기준으로 쌓여서요). 그럴 땐 한 번만 이렇게 편입하세요:

```bash
organum observatory sync --also ~/old-path/my-project
```

토큰 자리에 숫자 대신 `—`가 보이면 그건 0이 아니라 "그 벤더가 디스크에 안
남겨서 잴 수 없음"이라는 뜻이에요. organum은 모르는 값을 0으로 뭉개지 않아요.

## 이름 붙이기 (선택)

카드에 세션 해시 대신 이름과 목적이 보이면 통계가 훨씬 읽기 좋아져요.
에이전트 터미널에서 한 줄:

```bash
organum join --role dev --intent "결제 모듈 리팩터링" --for mycell
```

이러면 카드에 `mycell · dev`와 의도가 붙고, `stats --by role`이 "무슨 역할
세션이 얼마를 썼나"로 묶여요. 안 해도 관측은 다 돼요 — 정체성은 opt-in이에요.

## 경계, 한 번만 분명히

organum은 세션을 시작하지도, 멈추지도, 라우팅하지도 않아요. 읽기 전용 관측과
상태 축적이 전부고, 그래서 어느 워크플로에 붙여도 아무것도 깨지지 않아요.
쌓이는 데이터는 전부 그 프로젝트의 `.organum/` 로컬이고 기본으로 git에서
제외돼요(공유는 사용자가 선택할 때만).

## 자주 묻는 것

**Q. 지금 일하고 있는 이 Claude Code 터미널도 볼 수 있나요?**
네. 같은 폴더에서 (백그라운드로) `organum web`을 띄우거나, 다른 터미널에서
그 폴더로 가 띄우면 바로 그 세션이 카드로 떠요. 에이전트에게 시켜도 되고요.

**Q. 서브에이전트 토큰도 합산되나요?**
네. 서브에이전트는 별도 카드로 뜨고 상단 합계에 포함돼요. 부모 카드만 보면
실제 소비를 한참 놓치는데, 그걸 메우는 게 이 도구의 존재 이유 중 하나예요.

**Q. 예전 세션이 안 보여요.**
관제탑은 최근 30분 활동만 "살아있는" 것으로 보여줘요. 지난 세션들은
`observatory stats`로 보세요 — 단, transcript가 지워지기 전(약 30일 안)에
`sync`나 `checkup`이 한 번은 돌았어야 해요.
