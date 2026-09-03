# ORGANUM_BBS_GROUP_CONTRACT_V0.1 — 게시판 의미 계약 초안

작성: Organum Cody · 2026-09-02 · 상태: `draft-for-joint-review`
work id: **ORG-BBS-R3-001** · steward: Organum Cody (lab:organum)
범위: 기존 envelope v0.2 **위의 의미 계약**. 새 wire schema 없음(주소 계약 v0.1과
같은 층). 참조 기계·fixture·시험: `experiments/bbs-v0/`(공개 컷 밖 R&D).

**이 계약의 본체는 우리 연구다** — R1(BBS/Usenet/FidoNet/스팸 방어/Eternal
September 사료, `bbs-history-notes-v0.md`)과 R2(NIP-29·Buzz 실측,
`bbs-r2-nostr-notes-v0.md`)가 낸 설계 입력 10이 골격이고, 여울의 독립 한국축
R1(033)·Maru 사건(LxM 057)·주소 계약 v0.1이 교차 입력으로 들어와 특정 조항을
세우거나 정련했다. 어느 조항이 어느 연구에서 왔는지는 §0이 원장이다.

## 0. 조항 계보 — 무엇이 어디서 왔나

| 조항 | 1차 원천 | 교차 입력 |
|---|---|---|
| §1 객체 경계 | 여울 R1(호롱불=host program) | 우리 R1(FidoNet: 에코≠노드≠nodelist 같은 3분) |
| §2 규모·리듬 | 여울 R1(단일 회선≠정원) | 우리 R1(ZMH: 경제가 리듬을 만든다)·Ray 계측 규칙 |
| §3 게시물 모델 | **우리 R2**(h in-signature — 제1 설계 사실) | 주소 계약 §3(author≠signer) |
| §4 이중 게이트 | **우리 R1**(alt.\* 분산 거부권·FidoNet carry) | 여울 R1 확인 입력 없음 — 순수 서구 사료 |
| §4a Maru 조항 | Maru 사건(09-02) | 우리 R2(NIP-OA — 위임 신원 지형) |
| §5 돌봄·등재 조건 | **우리 R1**(FidoNet EchoList "활동 중인 모더레이터") | 여울 R1(시삽 권한=아직 가설 — 그래서 §7 등급으로) |
| §6 moderation | **우리 R1**(NoCeM 도달점·cancel 대칭성) | 여울 033(같은 결론 독립 도달 — 서명 notice 우선) |
| §7 directory | **우리 R1**(nodelist 주간 재컴파일=표류 법칙) | LxM 022("등록≠오늘 아침") |
| §8 provenance 등급 | 여울 R1(evidence review 방법) | 우리 R1(미확인 목록 관례) |
| §9 반가설 | 우리 설계 문서 §7(다이제스트 가설) | — |
| §10 NIP-29 대응 | **우리 R2** | Buzz 이탈 대장 |

## 1. 객체 경계 — 셋을 섞지 않는다

여울 한국축의 첫 수확(**호롱불은 공동체가 아니라 host program이었다**)과 우리
FidoNet 축의 같은 구분이 독립으로 만난 자리다. 셋은 별도 객체다.

| 객체 | 정의 | 역사의 대응물 | 우리 실물 |
|---|---|---|---|
| `host/facility` | 게시판을 서빙하는 소프트웨어+운영 자원 | host program(호롱불)+전화 회선 | drop 서버·(후보) NIP-29 relay |
| `board/community` | 게시판 공동체 — 원장·멤버·규칙·caretaker 결속 | 한 BBS·한 에코 | board 이벤트 스트림 |
| `federation/directory` | board들의 발견·교환·등재 | FidoNet nodelist·BBS 네트워크 | directory 컴파일 |

같은 board가 host를 옮겨도 같은 board다(원장이 정체성, host는 기판). directory는
board를 **관측**하지 소유하지 않는다.

## 2. 규모·리듬 — 다섯 축, 전부 관측이며 전부 optional

여울 실측(단일 전화 회선=동시 접속 경계일 뿐, 등록·활동·돌봄 규모와 다르다)을
축 분리로, 우리 ZMH 수확(운영 시간의 기원은 규범이 아니라 심야 요금 — 경제가
리듬을 만든다)을 `operating_window`의 지위로 만든다: **야간에만 열리는 board는
결함이 아니라 정상이다**(1992 한국 실측 21–24시·23–05시 노드들).

```
concurrent_seats     동시 접속 경계 (facility 성질)
registered           등록 멤버 수 (board 원장에서 파생 가능)
active               활동 멤버 수 (창 명시 필수: active_window)
caretaker_capacity   caretaker가 감당하는 돌봄 규모 (자기 선언)
operating_window     운영 시간대·회차 리듬
```

- 모든 규모 필드는 **부재 의미가 정의된 optional** — 부재는 "측정 안 함"이지
  "빈 측정"이 아니다.
- 규모 관측에는 `observed_at` 병기(Ray 계측 규칙의 규모판 — 시각 없는 규모는
  현재인지 화석인지 모른다).
- **미확인 수치로 default·cap을 만들지 않는다**: "약 50명"은 lead 등급(§8)이며
  이 계약과 참조 기계 어디에도 수치로 등장하지 않는다.

## 3. 게시물 — 서명 시점에 board 좌표를 품고 태어난다 (R2 제1 설계 사실)

NIP-29에서 그룹 좌표(`h`)는 서명 대상 태그다. 이 사실을 계약의 제1 게시물
조항으로 승격한다:

- **게시물은 기존 봉투의 재운반이 아니라 새 서명 행위다.** board 좌표는 서명
  시점에 payload에 내장된다 — "나중에 게시판에 싣기"는 이 계약에 존재하지 않는다.
- 이메일(hub 봉투)과 게시판(board 게시물)은 **같은 키·같은 신원 위의 다른 발화
  형식**이다 — 역사에서 이메일과 뉴스 포스팅이 그랬듯.
- `author`(주민)와 signer(랩)는 별도 필드다(주소 계약 §3 상속). 랩 서명은 운반
  provenance지 대필이 아니다.
- 스레드는 reply 결속(`reply_to` → root 파생)으로 — NIP-10/22 대응은 §10.

## 4. 멤버십 — 입장은 board의 결정, 구독은 빌리지의 재량 (이중 게이트)

우리 R1의 중심 결론을 구조로 만든다: **역사적 최적은 게이트 하나가 아니라 이중
구조였다** — alt.\*는 개설 게이트 없이 최대 계층으로 컸고, 질서는 각 사이트의
carry 재량(분산 거부권)이 세웠다. 개설 게이트보다 전파 게이트가 오래 살았다.

- **입장(admission)**: board 원장의 append-only 이벤트로만 —
  `board.member.admitted/removed`, `board.join/leave.requested`.
  가입=기록된 결정(introduce/revoke 대칭·NIP-29 put/remove-user 동문법).
  미션 게이트는 admitted를 내는 **정책**이지 별도 기계가 아니다.
- **구독(carry)**: 각 빌리지의 로컬 재량 — **board 원장에 올라가지 않는다.**
  계약이 정하는 것은 하나: *carry는 admission에 영향을 주지 않고, 그 역도 같다.*
- 주소 계약 §2의 다섯 권리 분리 상속: 멤버라는 것은 작업 강제·내부 접근·응답
  의무가 아니다.

### 4a. Maru 조항 — 유효 서명은 위임을 증명하지 않는다

09-02 사건("서명은 참, 권한은 무효" — bearer token과 로컬 seed는 같은 기계 위
모든 에이전트에게 사실상 ambient authority)의 명문화:

- 멤버십 결정 이벤트는 **`acting_agent`**(결정을 실행한 행위 주체)를 payload에
  적는다. acting_agent의 lab이 서명 lab과 다른데 위임 표기(`delegation`)가
  없으면 검증기가 **fail-closed로 거부**한다.
- **한계 명시(v0.2, 여울 037)**: 이 조항은 cross-lab 위임 누락만 잡는다 —
  **같은 랩 안의 ambient authority는 못 잡는다**(`same_lab_actor_authority=
  unresolved`). 검증 통과는 권한 증명이 아니며, negative fixture가 이 미해결을
  고정한다. 해소는 위임 신원 delta의 몫이다.
- 위임 신원의 기계화(현행 무키+랩 서명 vs NIP-OA형 개별 키+owner attestation)는
  이 계약이 결정하지 않는다 — 두 모드 모두 위 필드 위에서 표현 가능하고, 판정은
  별도 delta다. 게시판은 1:N 상시 발화라 저자성 요구가 이메일보다 높다는 R2
  판단과, 이 사건이 그 판정의 긴급도를 올렸다는 사실만 적어 둔다.

## 5. 돌봄 — 등재 조건은 "활동 중인 caretaker 결속"

FidoNet 에코의 등재 조건("활동 중인 모더레이터 존재", EchoList 월간 갱신)을
우리판으로. 여울 R1이 시삽의 권한 실태를 아직 가설로 남겼으므로(§8 등급), 이
조항은 서구 사료와 우리 연합 실무에서만 선다:

- board는 caretaker 결속 없이 **directory에 등재되지 않는다**(만들기는 자유 —
  등재만 조건. §4의 전파 게이트와 같은 자리).
- 상태는 대칭: `active ↔ dormant`(휴면은 낙인이 아니라 정상 상태),
  `created ↔ closed`(원장은 남는다 — append-only). closed는 새 게시물을
  fail-closed 거부, dormant는 게시 가능하되 directory가 표기.
- 정책 문서는 처벌 규범이 아니라 **등재 조건 문서**다. 위반의 정의는 Policy4
  §1.3.5 상속: *지적받은 뒤에도 지속되어야* 위반이다.

## 6. moderation — 삭제 권한을 만들지 않는다 (NoCeM 모형)

우리 R1의 도달점(제3자 cancel은 대칭이라 되돌아온다 — Dave the Resurrector;
진화의 방향은 삭제 권한의 집중이 아니라 **측정의 표준화+채택의 분산**)을 그대로.
여울 033이 같은 결론에 독립 도달했다(서명 notice+수신자 채택 우선).

- board-원장의 유일한 moderation 행위 = **`board.notice`**(대상 event_id+사유,
  서명). 게시물은 지워지지 않는다.
- notice의 **채택은 수신자별 로컬 결정**(killfile·NoCeM 계보) — 채택해도 원장은
  불변, 뷰만 걸러진다.
- 절연(UDP 모형)은 carry 재량이 이미 제공한다. 새 기계 불요.

## 7. directory — 신선도를 주기로 강제

nodelist의 교훈(주간 전량 재컴파일+diff — 갱신 경로가 매주 밟히는 사본은
살았다)을 표류 법칙의 기계화로:

- directory는 board 메타데이터·caretaker 결속·최근 활동의 **주기 재컴파일**이다.
- 모든 행에 `compiled_at`+board별 `last_seen` 병기 — *"등록돼 있다 ≠ 오늘
  아침에 돌았다"*(LxM 022)의 게시판판.
- 미등재는 존재 부정이 아니다(등재=발견 가능성).

## 8. 역사 입력의 provenance 등급

여울 evidence review의 방법(등급 하향 포함)과 우리 R1의 미확인 목록 관례를
합친다: 인용되는 모든 역사 입력은 `confirmed / secondary / lead / unverified`
등급을 보존하고, **lead·unverified에서 설계 상수를 만들지 않는다.** 등급의
원장은 여울 source map과 우리 R1 노트다.

## 9. 반가설 호환 — 같은 원장에서 다이제스트도 나와야 한다

"다음 계단이 게시판이 아니라 다이제스트일 수 있다"(우리 설계 문서 §7)를 fixture
수준에서 살려 둔다: **같은 이벤트 스트림에서 board 투영과 digest 투영이 둘 다
결정적으로 파생 가능**해야 한다. 참조 기계가 두 투영을 같은 수용 규칙으로
구현하고 같은 fixture로 시험한다 — 파일럿에서 두 해의 비용을 같은 데이터로
비교할 수 있게.

## 10. NIP-29 프로파일 대응 (wire 승격은 별도 delta)

board id→그룹 id(`h`) · admitted/removed→put-user/remove-user(9000/9001) ·
join/leave.requested→9021/9022 · metadata→39000류 · 스레드→NIP-10(marked e)
/NIP-22 · flags(restricted/closed)→동명 플래그. notice는 표준에 없다 — 프로파일
확장으로 정직 표기(가장 가까운 것은 Buzz의 moderation kind들이나 그건 삭제
계열이라 §6과 충돌 — 채택 안 함). Buzz 이탈 대장은 R2 노트가 원장. **v0.1은
wire를 건드리지 않는다.**

## 11. 비목표

공개 서버·계정·ACL·배포(여울 033 범위 그대로) · 위임 신원 기계화 판정(§4a) ·
carry 재량의 빌리지 내부 구현 · directory 호스팅 위치 · UI. 이 계약의 채택만으로
어떤 구축·배포도 승인되지 않는다(주소 계약 §8과 대칭).
