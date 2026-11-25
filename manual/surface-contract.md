# organum 표면 계약 및 `1.x` 안정성 정책 (Surface Contract)

이 문서는 `organum` 패키지의 **안정 표면(Stable)** 과 **실험 표면(Experimental)** 의 경계, `--json` 스키마·종료코드 계약, Deprecation 주기, `2.0.0` 트리거 조건 및 긴급 패치 정책을 정의합니다.

---

## 1. Stable 표면 vs Experimental 표면

### A. Stable 표면 (`[stable]` — `1.x` 하위 호환성 보장 대상)

| 표면 | 진입점 / 명령 | 역할 | 포맷 / 스키마 계약 |
|---|---|---|---|
| **Inspector** | `organum-inspector` · `organum inspector` | 다중 벤더 세션 사후 계측(read-only) | `--json` 집계 구조 보존 |
| **Hub** | `organum-hub` | 서명 증거 봉투·투명 로그·최소 HTTP 드롭 | `organum-hub/envelope/v0.2` (동결) |
| **BBS** | `organum-bbs` | 서명 기반 게시판·스레드·전화번호부 | `bbs-group-contract-v0.1` · `profile-directory-contract-v0.1` |
| **Occupancy** | `organum occupancy` | 기억 원장 시간 버킷 점유 진단(read-only) | `organum.temporal-occupancy/v1` |
| **Manifest** | `organum manifest check` | 커버 `## Files` 표 vs 실제 파일 검증(read-only) | `organum.record-manifest/v1` |
| **SemVer Gate** | `organum semver-gate check\|transition` | SemVer 2.0.0 / CalVer 제약·전이 게이트(read-only) | `organum.semver-gate/v1` |
| **Chronicle** | `organum chronicle check\|diff` | Git blob SHA-1 기반 YAML·MD 원장 검증/diff(read-only) | `organum.chronicle/v1` |
| **Rule Audit** | `organum rule-audit demo\|check` | 독립 불변조건 감사·`legacy_verdict` escape 탐지(read-only) | `organum.rule-audit/v1` |
| **Zero-Ref** | `organum zero-ref check\|batch` | 영점 기준 일치 검사(arXiv 2611.27186 §2.2, read-only) | `organum.zero-ref/v1` |

### B. Experimental 표면 (`[experimental]` — 현장 연구·진화 중)

`organum --help`에 `[experimental]`로 표기된 명령들은 살아있는 에이전트 마을과 연구 현장에서 검증 중인 표면입니다:
- 상태·기억·회고: `init`, `context`, `remember`, `recall`, `map`, `distill`, `reflect`, `migrate`, `checkup`, `backup`, `restore`, `provision`
- 라이브 관측·관제: `inspect`, `live`, `web`, `bench`, `observatory`
- 로컬 조율 필드: `mcp`, `relay`, `roster`, `agora`, `hub` *(로컬 `~/.organum/hub/` 프로토타입 — 정식 서명 봉투 CLI는 `organum-hub`)*, `alarm`, `session`, `join`

> `[experimental]` 명령의 플래그·출력·내부 구조는 마이너 버전에서도 예고 후 변경·졸업·폐기될 수 있습니다. 단, `.organum/` `format-v0`에 이미 기록된 사용자 데이터는 파괴하지 않으며 포맷 변경 시 자동 백업 기반 `migrate` 경로를 제공합니다.

---

## 2. Stable `--json` 스키마 및 종료코드 계약

자동화 스크립트와 CI 파이프라인은 사람이 읽는 텍스트 출력 대신 **`--json` 출력과 종료코드**에 의존해야 합니다.

| 명령 | `schema` 값 | 필수 최상위 키 (삭제·의미 변경 금지) | 종료코드 |
|---|---|---|---|
| `organum occupancy --json` | `organum.temporal-occupancy/v1` | `schema`, `source`, `total_items`, `skipped_rows`, `capacity`, `bucket_cap`, `kept_count`, `distinct_buckets`, `max_bucket_occupancy`, `evicted_bucket_cap`, `evicted_global_cap` | `0` 정상 · `1` 파일/파싱 오류 |
| `organum manifest check --json` | `organum.record-manifest/v1` | `schema`, `verdict`, `passed`, `named_count`, `listed_count`, `missing_count`, `findings`, `directory`, `cover` | `0` 통과 · `1` 누락/오류 |
| `organum semver-gate check --json` | `organum.semver-gate/v1` | `schema`, `ok`, `reason_code`, `detail`, `version_raw`, `spec_raw` | `0` 만족 · `1` 불만족/오류 |
| `organum semver-gate transition --json` | `organum.semver-gate/v1` | `schema`, `status`, `alert`, `detail`, `before_raw`, `after_raw` | `0` 정상 전이 · `1` 경보(`alert: true`) |
| `organum chronicle check --json` | `organum.chronicle/v1` | `schema`, `source_path`, `blob_fingerprint`, `entry_count`, `entries` | `0` 정상 · `1` 문법/중복/오류 |
| `organum chronicle diff --json` | `organum.chronicle/v1` | `schema`, `before`, `after`, `counts`, `added`, `removed`, `changed` | `0` 정상 · `1` 파싱/오류 |
| `organum rule-audit demo --json` | `organum.rule-audit/v1` | `schema`, `pack`, `summary`, `cases` | `0` 자기검증 통과 · `1` 오탐/미탐 |
| `organum rule-audit check --json` | `organum.rule-audit/v1` | `schema`, `pack`, `passed`, `escape_detected`, `rules_checked`, `violations` | `0` 통과 · `1` 위반/오류 |
| `organum zero-ref check --json` | `organum.zero-ref/v1` | `schema`, `mode`, `classification`, `informative`, `candidate_pass`, `zero_pass`, `t_zero`, `margin`, `zero_at_boundary` | `0` `informative_match` · `1` 그 외/오류 |
| `organum zero-ref batch --json` | `organum.zero-ref/v1` | `schema`, `mode`, `summary`, `results` | `0` 전건 `informative_match` · `1` 그 외/오류 |

**`1.x` 비파괴(Additive-Only) 규칙**:
- Stable 명령의 `--json` 출력에 **새 선택적 키를 추가**하는 것은 허용됩니다(소비자는 알 수 없는 키를 무시해야 합니다).
- 필수 키의 삭제·이름 변경·타입 변경·의미 변경은 금지되며, `schema`의 `/v1` 접미가 유지되는 한 계약을 지킵니다.

---

## 3. CLI 언어 방침

- **기계 계약(항상 ASCII 영문)**: 명령 이름, 플래그, `--json` 키, `schema` 식별자, `reason_code`, `status`, `classification`, 종료코드.
- **사람용 출력(`--help` · 비-JSON 요약)**: 현재 한국어·영문 혼용을 유지하되, **기계 파싱 대상이 아닙니다.** 사람용 문구의 띄어쓰기·어미·번역 개선은 파괴적 변경(breaking change)으로 보지 않습니다.
- **공식 매뉴얼(`manual/`)**: 모든 Stable 표면에 대해 한국어(`.md`)와 영문(`.en.md`) 매뉴얼을 함께 제공합니다.

---

## 4. `1.x` 호환성 · Deprecation · `2.0.0` · 긴급 패치 정책

1. **런타임 헌법**: Python `>=3.10`, **외부 런타임 의존성 0(stdlib-only)**, 감지 ≠ 자동 변조(read-only 검증 기관은 입력 파일을 수정하지 않음).
2. **Deprecation 주기**:
   - Stable 플래그·옵션·동작을 폐기하려면 먼저 최소 **2개 마이너 릴리스 또는 90일(둘 중 긴 기간)** 동안 `stderr` 경고와 릴리스 노트에 대체 경로를 공지합니다.
   - 실제 제거는 메이저 버전(`2.0.0`)에서만 수행합니다.
3. **`2.0.0` 트리거 조건**:
   - Stable CLI의 기존 플래그 삭제, 종료코드 의미 변경, 필수 JSON 키 삭제/타입 변경.
   - `organum-hub` / `organum-bbs` 와이어 봉투의 하위 비호환 변경.
   - 공식 EOL 이전의 Python 최소 버전 상향(단, Python 공식 지원 종료 버전 제외는 마이너 버전에서 사전 예고 가능).
4. **긴급 보안·무결성 패치 및 PyPI Yank 절차**:
   - 서명 검증 우회, 데이터 손실, 경로 탈출 등 중대 결함 발견 시 즉시 패치 릴리스(`x.y.(z+1)`)를 출하하고 영향 버전을 PyPI에서 **yank**(사유 명시)합니다.
   - Git 태그·커밋 이력은 절대 덮어쓰거나 삭제하지 않습니다.

---

## 같이 보기

- [inspector 퀵스타트](quickstart-inspector.md) · [hub 퀵스타트](quickstart-hub.md) · [bbs 퀵스타트](quickstart-bbs.md) · [검증·위생 기관 6종 퀵스타트](quickstart-verify.md)
