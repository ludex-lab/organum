# organum 검증·위생 기관 6종 퀵스타트 (0.11.0)

`organum 0.11.0`은 에이전트 팀이 일상 운영과 연구에서 반복적으로 밟는 6가지 조용한 실패(기억 단작·누락 파일·버전 전이 오판·원장 훼손·검증기 누출·공허한 통과)를 잡는 **읽기 전용(read-only) · 표준 라이브러리 전용** 검증 기관 6종을 추가했습니다.

공통 원칙 셋:
1. **감지 ≠ 자동 변조**: 6개 명령 모두 입력 파일·원장·저장소를 수정하지 않습니다.
2. **외부 의존성 0**: Python `>=3.10` 표준 라이브러리만으로 오프라인·결정적으로 동작합니다(`pip install organum`).
3. **기계 판독 스키마(`--json`)**: 모든 명령이 `organum.<name>/v1` JSON 스키마와 명확한 종료코드(`0`/`1`)를 돌려줍니다.

| 명령 | 한 줄 요약 | `--json` 스키마 | 종료코드 |
|---|---|---|---|
| `organum occupancy` | 기억 원장의 시간 버킷 집중(단작) 진단 | `organum.temporal-occupancy/v1` | `0` 정상 · `1` 파일/파싱 오류 |
| `organum manifest check` | 커버 `## Files` 표와 실제 파일·영수증 대조 | `organum.record-manifest/v1` | `0` 통과 · `1` 누락/오류 |
| `organum semver-gate` | SemVer 2.0.0 / CalVer 제약·업그레이드 전이 분류 | `organum.semver-gate/v1` | `0` 통과/정상 · `1` 미달/경보 |
| `organum chronicle` | Git blob SHA-1 지문 기반 YAML·MD 원장 검증/diff | `organum.chronicle/v1` | `0` 정상 · `1` 문법/중복/오류 |
| `organum rule-audit` | 독립 규칙 팩으로 trace 감사 + `legacy_verdict` escape 탐지 | `organum.rule-audit/v1` | `0` 통과 · `1` 위반/오류 |
| `organum zero-ref` | 영점(`0`) 기준 일치 검사(arXiv 2611.27186 §2.2, 정확 `Decimal`) | `organum.zero-ref/v1` | `0` 정보성 통과 · `1` 공허/불일치/오류 |

---

## 1. `organum occupancy` — 기억 원장 시간 버킷 점유 진단

짧은 폭주 구간에 기억이 몰려 오래된 맥락이 전부 밀려나는 **시간 단작(burst monoculture)** 을 원장을 건드리지 않고 측정합니다.

```bash
# 기본: .organum/memory/memories.jsonl 진단 (24슬롯, 24시간 버킷, 버킷당 상한 6)
organum occupancy

# 임의 JSONL 원장을 1시간 버킷·상한 4로 진단하고 JSON 출력
organum occupancy --file path/to/facts.jsonl --capacity 32 --bucket-hours 1 --bucket-cap 4 --json
```

- 타임스탬프 필드는 `ts` · `created_at` · `at` · `timestamp` 순으로 자동 인식하며, 파싱 불가 행은 조용히 버리지 않고 `skipped_rows`로 집계합니다.
- **정직 경계**: 진단 전용입니다. 실제로 원장을 압축·삭제하거나 보존 정책을 집행하지 않습니다.

---

## 2. `organum manifest check` — 기록 폴더 커버(`## Files`) 존재 검증

연구·감사 폴더의 커버 문서(`README.md` 등) 안 `## Files` 표에 적힌 파일들이 실제 디렉터리에 존재하는지 검증합니다.

```bash
# 현재 디렉터리의 README.md(## Files) 검증
organum manifest check .

# .gitignore 누락 사유 귀속 + 필수 영수증 확인 + JSON 출력
organum manifest check ./audit-bundle --cover README.md --gitignore .gitignore --receipt-name RECEIPT.json --json
```

- 중괄호 확장(`logs/{a,b}.txt`), macOS NFD ↔ Linux NFC 정규화 차이, `.gitignore`에 가려져 빠진 파일 귀속을 자동 처리합니다.
- **정직 경계**: 파일 **존재**와 표·폴더 일치만 검사합니다. 파일 내용의 진위나 암호학적 해시 검증은 `organum-hub attest` 몫입니다.

---

## 3. `organum semver-gate` — SemVer 2.0.0 · CalVer 범위 및 전이 게이트

에이전트 하네스·도구 업그레이드 시 prerelease 함정, 메이저 점프, 다운그레이드, CalVer(`YYYY.MM.DD`) 체계를 결정적으로 판정합니다.

```bash
# 안정 범위 검사 (prerelease는 --allow-prerelease 없이는 안정 범위 통과 불가)
organum semver-gate check 1.4.2 ">= 1.0.0, < 2.0.0"
organum semver-gate check 2.0.0-rc.1 ">= 1.0.0" --json   # exit 1 (prerelease 거부)

# CLI --version 출력 두 줄의 업그레이드 전이 분류 (cursor 계열은 자동 CalVer)
organum semver-gate transition "codex-cli 0.112.0" "codex-cli 0.114.0" --floor ">= 0.100.0" --json
organum semver-gate transition "2026.10.01-1a2b3c4" "2026.11.20-9f8e7d6" --tool cursor-agent --json
```

- 전이 분류가 경보(`alert: true` — 다운그레이드, floor 미달, 파싱 불가 등)이면 `exit 1`, 정상 전이이면 `exit 0`을 반환합니다.
- **정직 경계**: 버전 문자열과 정책 범위만 판정합니다. 실제 바이너리 기능 호환성이나 원격 릴리스 메타데이터는 조회하지 않습니다.

---

## 4. `organum chronicle` — Git blob 지문 기반 원장·연대기 검증 및 diff

평탄한 YAML 원장(예: 스위치 목록, 상태 레지스트리)이나 날짜 머리글이 있는 Markdown 연대기를 파싱해 **중복 ID·잘못된 날짜**를 잡고, Git과 동일한 `sha1("blob <len>\0" + bytes)` 지문과 항목 단위 diff를 출력합니다.

```bash
# 파일 문법·중복 ID·날짜 검사 및 Git blob SHA-1 지문 확인
organum chronicle check ledger.yaml --json

# 두 스냅샷 파일 비교
organum chronicle diff before.yaml after.yaml

# Git 저장소 두 리비전 사이의 특정 원장 파일 비교
organum chronicle diff HEAD~10 HEAD --git-repo . --rel-path state/switches.yaml --json
```

- `--format`은 `auto` · `yaml` · `switches-yaml` · `markdown`을 지원하며, YAML 루트 키는 `--root-key`로 고정할 수 있습니다.
- **정직 경계**: 평탄한 목록형 YAML과 날짜 기반 Markdown 연대기에 맞춘 결정적 파서입니다. 임의 중첩 YAML(앵커·멀티라인 블록) 범용 파서가 아닙니다.

---

## 5. `organum rule-audit` — 독립 불변조건 감사 ("Verify the Verifier")

기존 검증기가 `PASS`를 찍고 놓친 결함(**escape**)을 독립 규칙 팩으로 사후 감사합니다.

```bash
# 내장 FIFO 큐 자기검증 데모 (정상·결함·legacy PASS escape 탐지 확인)
organum rule-audit demo
organum rule-audit demo --json

# trace JSON 파일을 hygiene 팩으로 감사하며 기존 판정(PASS)의 escape 여부 확인
organum rule-audit check trace.json --pack hygiene --legacy-verdict PASS --json
```

- 내장 팩: `queue`(FIFO 순서·중복·유실 불변조건) · `hygiene`(에이전트 실행 trace 위생 불변조건).
- `legacy_verdict=PASS`인데 규칙 위반이 나오면 `escape_detected: true`와 함께 `exit 1`을 반환합니다.
- **정직 경계**: 선언된 규칙 팩 범위 안의 위반만 잡습니다. 규칙 팩 자체가 빠뜨린 결함 유형은 잡지 못하므로, `pass`는 "결함 없음"이 아니라 "이 팩의 규칙 위반 0건"을 뜻합니다.

---

## 6. `organum zero-ref` — 영점(`0`) 기준 일치 판정 검사

신뢰구간 `[lo, hi]`와 허용 오차 `m`이 너무 넓어서 **아무 효과 없는 영점 후보(`0`)조차 통과해 버리는 공허한 합격(`uninformative_pass`)** 을 적발합니다(arXiv 2611.27186 §2.2, `T = max(|lo|, |hi|)`).

```bash
# 단건 검사 (기본: 논문 §2.2 엄격 부등호 < ; 포함 <= 는 --inclusive)
organum zero-ref check --lo 0.50 --hi 0.80 --candidate 0.65 --margin 0.25 --json
# → informative_match (exit 0: candidate PASS, zero FAIL, T=0.80 >= m=0.25)

organum zero-ref check --lo -0.10 --hi 0.20 --candidate 0.05 --margin 0.25 --json
# → uninformative_pass (exit 1: candidate PASS이지만 zero도 PASS, T=0.20 < m=0.25)

# JSON 배열 또는 JSONL 일괄 검사
organum zero-ref batch claims.jsonl --json
```

- 모든 경계 비교는 부동소수점 오차를 피하기 위해 Python `Decimal` 정확 산술로 계산합니다(`T == m` 경계 행은 `zero_at_boundary: true`로 별도 표기).
- 분류 4종: `informative_match`(유일한 `exit 0`) · `uninformative_pass` · `worse_than_zero` · `mismatch`.
- **정직 경계**: 구간과 마진의 기하적 정보성만 판정합니다. 입력 구간 `[lo, hi]` 자체의 통계적 타당성이나 표본 크기는 검증하지 않습니다.

---

## 같이 보기

- 세션 계량: [inspector 퀵스타트](quickstart-inspector.md)
- 서명 증거 봉투·우체통: [hub 퀵스타트](quickstart-hub.md)
- 게시판·전화번호부: [bbs 퀵스타트](quickstart-bbs.md)
