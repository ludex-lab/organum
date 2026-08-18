# organum hub — 서명 증거 봉투 5분 퀵스타트

*English version: [quickstart-hub.en.md](quickstart-hub.en.md)*

여러 에이전트 작업공간(랩)이 서로에게 뭔가를 주장할 때 — "이 파일을 동결했다",
"이 측정을 이 도구 버전으로 돌렸다" — 나중에 누구든 검증할 수 있는 형태로 남기고
싶을 때가 있어요. organum hub는 그 층입니다. **서명 봉투 + 서명 영수증 + 투명
로그**로 "누가, 언제(수용 순서), 무엇을" 주장했는지가 위조·소급 없이 남아요.

hub가 **아닌 것**도 분명히 할게요: 대화 채널이 아니고(본문을 싣지 않아요 —
locator와 digest만), 서버도 아니에요(순수 파이썬 라이브러리 + 선택형 어댑터).
transport는 뭐든 됩니다 — 파일로 나르든, Nostr relay에 태우든.

> **실험 단계(experimental)** 예요. 스펙은 3랩 크리틱을 거쳐 로컬 기준으로
> 동결됐지만(봉투 v0.2 · wire v1), 잔여 목록이 명시돼 있어요(맨 아래).
> 외부 의존성 0 — 표준 라이브러리만 씁니다.

## 설치

```bash
pip install organum        # 0.3.0+ — hub 모듈·organum-hub CLI 포함
```

## 5분 컷: 봉투 하나가 영수증까지 가는 길

```python
import hashlib
from organum import hub_envelope as he
from organum import hub_log as hl
from organum import schnorr_pure as sp

# 1) 키 — 랩 서명키와 허브 영수증키 (데모용 seed. 실전은 OS keystore에)
lab_seed = hashlib.sha256(b"my-lab-demo-seed").digest()
hub_seed = hashlib.sha256(b"my-hub-demo-seed").digest()
lab_pub = sp.public_key(lab_seed).hex()

# 2) 레지스트리 — 누가(키) 무엇을(클레임 종류) 주장할 수 있나
keys = he.KeyRegistry()
keys.register(lab_pub, signer_id="lab:demo", key_id="k1", key_epoch=1)
claims_doc = {"claims": {
    "core:artifact.frozen": {
        "act_class": "self_attesting", "subject_types": ["artifact"],
        "ordering_levels": ["emission"], "capture_required": False,
        "revocation_authority": "same_signer"}}}
claims = he.ClaimRegistry(claims_doc, expected_sha256=he.canonical_sha(claims_doc))

# 3) 허브 — 수용·투명 로그·서명 영수증이 한 몸
hub = he.HubIndex(key_registry=keys, claim_registry=claims,
                  log=hl.TransparencyLog(), receipt_seckey=hub_seed,
                  source_domain="demo-hub/local")

# 4) 봉투 — "이 파일(sha256)을 동결했다"는 주장
env = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "artifact.attested",
       "signer": {"id": "lab:demo", "key_id": "k1", "key_epoch": 1},
       "subject": {"type": "artifact", "id": "artifact:prereg-v1"},
       "provenance": {"lab": "lab:demo", "machine": "m-01", "platform": "darwin",
                      "adapter": "organum-hub/0.1", "cli_version": None, "capture": None},
       "idempotency_key": "demo-1", "created_at": "2026-08-16T00:00:00Z",
       "payload": {"artifact": {"role": "prereg", "schema_id": "demo/v1",
                                "sha256": "a" * 64, "byte_length": 1234,
                                "media_type": "application/json"},
                   "bindings": [], "causal": {},
                   "claim": {"type": "core:artifact.frozen", "scope": "demo",
                             "attests_ordering_of": "emission",
                             "evidence_basis": {"method": "raw-bytes-sha256",
                                                "verifier_schema": "demo/v1",
                                                "body_custody": "repo",
                                                "locator_authority": False}}}}
raw = he.canonical_bytes(env)                          # 이 bytes가 곧 identity
sig = sp.sign(hashlib.sha256(raw).digest(), lab_seed)  # 랩이 서명

# 5) 수용 → 서명 영수증
r = hub.admit(raw, sig.hex(), lab_pub)
print(r["admitted"], r["accepted_seq"])                # True 1
rc = r["receipt"]
he.verify_relay_receipt(rc, sp.public_key(hub_seed),
                        expected_source_domain="demo-hub/local")   # True
```

이제 세 가지를 직접 확인해 보세요 — hub의 성격이 이 셋에 있어요.

```python
# 변조는 안 들어와요 — byte 하나만 달라도 서명 단계에서 격리
bad = raw[:-2] + b'"}'
hub.admit(bad, sig.hex(), lab_pub)["admitted"]         # False

# 재전송은 새 이벤트가 아니에요 — 최초 수용으로 수렴 (같은 seq, 같은 영수증)
hub.admit(raw, sig.hex(), lab_pub)["duplicate"]        # True

# 영수증의 root는 말이 아니라 증명이에요 — 포함 증명이 실제로 검증됩니다
body = rc["body"]
proof = hub.log.inclusion_proof(body["accepted_seq"] - 1, body["tree_size"])
hl.verify_inclusion(raw, body["accepted_seq"] - 1, body["tree_size"],
                    proof, bytes.fromhex(body["root"]))            # True
```

## CLI로 하면: 두 집단 교환 루프

라이브러리 없이 명령만으로도 위 전부가 돼요. 두 집단(A가 hub 운영, B는 외부)이
봉투를 주고받는 루프:

```bash
# A 쪽 — 키·허브 준비
organum-hub keygen lab-a && organum-hub keygen hub-receipt
organum-hub init --dir hub --source-domain lab:a/hub
organum-hub register-key --dir hub --signer lab:a --key-id k1 --epoch 1 \
    --pubkey $(cat lab-a.pub)
organum-hub register-key --dir hub --signer lab:b --key-id k1 --epoch 1 \
    --pubkey <B가 보낸 pubkey>

# A가 파일을 attest — 수용 + 서명 영수증
organum-hub attest --dir hub --key lab-a.seed --signer lab:a --key-id k1 --epoch 1 \
    --file prereg.json --claim core:artifact.frozen --scope demo \
    --receipt-key hub-receipt.seed

# B가 보낸 봉투를 admit (B는 자기 쪽에서 `organum-hub sign`으로 서명해 보냄)
organum-hub admit --dir hub --envelope b-envelope.json --sig <hex> --pubkey <hex>

# 포함 증명을 떼서 B에게 — B는 오프라인으로 검증
organum-hub prove --dir hub --event-id <id> > proof.json
organum-hub verify-proof --envelope a-envelope.json --proof proof.json

# 키 회전·폐기 — 이력이 남고 "그때 유효했나"를 물을 수 있어요
organum-hub rotate-key --dir hub --key lab-b.seed --signer lab:b --key-id k1 --epoch 1 \
    --new-key-id k2 --new-epoch 2 --new-pubkey <hex>
organum-hub revoke-key --dir hub --key lab-b2.seed --signer lab:b --key-id k2 --epoch 2 \
    --revoke-key-id k1 --revoke-epoch 1
organum-hub was-valid --dir hub --pubkey <hex> --seq 2   # 과거 좌표의 유효성
```

상태 모델이 단순해요: **로그가 곧 상태**입니다. 디렉터리엔 설정(`hub.json`)과
append-only 이벤트 로그(`events.jsonl`)뿐이고, 매 실행이 로그를 재생해 상태를
복원해요 — 로그를 변조하면 재생이 멈춥니다.

배포 형태도 이게 기본이에요: **각자 설치, 서버 없음.** 봉투+서명은 아무 경로로나
나르면 되고(파일·기존 채널), 서명이 origin을 보증하니 전송로는 신뢰 대상이
아니에요. 정기적으로 주고받기 시작하면 아래의 HTTP 우체통 하나로 충분하고,
상시·다자로 잦아지면 표준 Nostr relay를 어느 쪽이든 띄우면 됩니다 — 어느 쪽이든
운반자지 authority가 아니라서 신뢰를 요구하지 않아요.

## 전달을 HTTP로 — git도 relay도 없이 (drop v0)

봉투를 나르는 데 공유 git repo나 Nostr relay가 꼭 필요한 건 아니에요. 한쪽(또는
중립 호스트)이 **HTTP 우체통** 하나를 올리면, 상대는 주소와 토큰만 알면 됩니다:

```bash
# 호스트 쪽 — 우체통 서버 (의존성 0, 프로세스 하나, 내리면 그만)
python3 -c "import secrets; print(secrets.token_hex(32))" > tokens.txt
organum-hub serve --root drops --token-file tokens.txt --bind 0.0.0.0 --port 8642

# 보내는 쪽 — export가 만든 quad를 그대로 POST
organum-hub export --dir hub --out from-ray --body greeting.md
organum-hub push --url http://HOST:8642/v0/first-contact/from-ray \
    --quad from-ray/001 --token-file tokens.txt

# 받는 쪽 — 새 quad를 폴링으로 수신 → 평소처럼 admit
organum-hub pull --url http://HOST:8642/v0/first-contact/from-ray \
    --dest from-ray --token-file tokens.txt
organum-hub admit --dir hub --envelope from-ray/001-envelope.json \
    --sig-file from-ray/001-sig.txt --pubkey <B가 보낸 pubkey>
```

서버는 일부러 멍청해요: 봉투를 열지도 검증하지도 않고, git 우체통과 **같은
`from-x/NNN-*` 트리**를 물화할 뿐이에요. 위조·변조·순서는 봉투(서명·seq·digest)가
막고, 토큰은 쓰기 접근(스팸 방지)만 지고, 읽기 기밀은 배치(사설망/TLS)가 집니다 —
사설 git repo와 같은 노출 등급이에요. 같은 quad 재전송은 dedup으로 수렴하고,
같은 번호에 다른 내용이 오면 409로 거부돼요(먼저 쓴 것이 남습니다). 상대 주소가
**(URL, pubkey)** 한 쌍이라는 게 요점이에요 — pubkey가 신원이고 URL은 라우팅일
뿐이라, carrier를 바꿔도 신원과 검증은 그대로예요.

### 수신 규율 둘 (0.4.5)

받은 봉투를 장부에 넣기 전에 쓸 도구가 둘 늘었어요. **`verify-envelope`**는
hub 디렉터리 없이 서명·event_id·스키마·body digest·수신자(target)만 검증합니다 —
장부에 아무것도 남기지 않아요(`ledger_touched: false`). 공개키를 처음 교차
확인할 때, 회람 봉투를 게이트 수준으로만 볼 때 쓰는 표준 도구입니다. 그리고
**admit은 수신자가 아니면 기본 거부**합니다 — 봉투의 target lab이 내 hub의 운영
lab과 다르면(판정이 안 서는 hub도 마찬가지로) 로그를 전진시키지 않고 거절해요.
남의 봉투를 회람 증인으로 수용하려면 `--accept-foreign-target`을 명시합니다 —
수용이 실수가 아니라 결정이 되도록.

### 우체통을 상시로 올린다면 (0.4.1)

기본 경로는 언제나 self-host예요: 한쪽만 인바운드가 되면 성립하고, 비용도 서버
신뢰도 추가되지 않아요. 그런데 양쪽 다 NAT 뒤라면(움직이는 배 위의 랩톱을
상상해보세요) 터널을 뚫거나, **중립 호스트에 우체통을 올리는** 길이 있습니다.
그때 지켜야 할 게 셋이에요:

- **개방이 아니라 게이트**: 중립 호스트 우체통은 토큰 발급으로 멤버십을 닫아요.
  0.4.1부터 토큰(=멤버)별 분당 예산이 걸립니다 — 기본 60, 초과는 `429`와
  `Retry-After`로 답해요. 비용이 멤버 수에 비례해 유계로 남죠. 자기들끼리 쓰는
  self-host라면 `--rate-limit 0`으로 꺼도 됩니다.
- **durable 층은 배치 몫**: `--root`가 우체통의 유일한 상태예요. persistent
  disk를 붙이든 외부 스토리지로 미러하든, 유실 창이 있다면 유계이고 탐지
  가능해야 해요. 복구는 발신 쪽 재푸시가 집니다 — **회신이 없으면 재푸시하세요.**
  dedup이 멱등이라 언제나 안전해요.
- **정직 한 줄**: sealed 층이 오르기 전까지, 중립 호스트의 봉투 본문은 호스트
  운영자가 읽을 수 있어요. 신뢰하지 않는 서버에는 비밀을 싣지 않습니다.

작은 성질 둘도 적어둘게요(0.4.2). 서버는 dumb이지만 **위생은 지킵니다** — quad의
모양(3~6자리 번호, hex128 서명, 크기 캡)이 어긋나면 400으로 거절해요. 그리고
클라이언트 타임아웃 기본이 90초입니다 — 무료 호스팅의 콜드스타트(~1분)를 실측으로
반영한 값이고, `--timeout`으로 조절해요. 그래도 첫 요청이 끊기면 그냥 다시
보내세요 — 재푸시는 dedup이라 언제나 안전합니다.

## Nostr relay에 태우기 (선택) — Buzz 호환

wire 형식은 organum 전용이 아니라 **표준 Nostr**(NIP-01 직렬화 + BIP-340 서명)예요.
그래서 Nostr 생태계의 relay가 그대로 운반자가 됩니다 — 대표적으로 **Buzz**(Block의
오픈소스 에이전트 워크스페이스, Nostr 기반)와의 상호운용을 실제 relay를 띄워 검증했어요:
봉투 publish 수용, 저장 거쳐 byte-identical 복원, 거부 의미론 일치까지
(`ghcr.io/block/buzz@sha256:32937a66…`, 2026-08-15 빌드 기준).

관계를 정확히 하면 — hub는 Buzz의 경량판이 아니라 **다른 층**이에요. Buzz는
채널·워크스페이스를 주고, hub는 서명 증거·검증을 줍니다. 두 층이 표준 wire에서
만나기 때문에, Buzz를 쓰는 커뮤니티와도 relay 하나로 봉투를 주고받을 수 있고,
Buzz 없이도(파일 교환, 또는 아무 경량 NIP-01 relay) 전부 돌아가요 — 의존성은
계속 0입니다.

```python
from organum import hub_wire as hw

ev = hw.build_wire_event(raw, seckey=lab_seed, created_at=1755300000)
# → kind 1 + [["t","organum-hub-v1"]] 태그의 표준 Nostr event. relay에 publish.

# 수신 쪽: relay가 준 event를 그대로 —
r = hw.admit_wire(hub, ev)     # wire 서명 검증 → carrier 확인 → 위의 admit과 같은 경로
r["receipt"]["body"]["wire_event_id"]      # wire와 봉투, 두 identity를 영수증이 결속
```

wire 서명(NIP-01)이 outer authority예요 — 수신자가 다시 서명할 일은 없습니다.
같은 봉투가 다른 `created_at`으로 재전송돼 wire id가 달라져도 최초로 수렴해요.

## 피험체 보호 한 조각

측정당하는 에이전트의 컨텍스트에 hub 내용이 흘러들면 측정이 오염돼요. 그걸
선언이 아니라 구조로 막는 assert가 있어요:

```python
he.assert_source_allowlist(["task:mission"], allowlist=("task:mission",))  # 통과
he.assert_source_allowlist(["plane:coordination"],
                           allowlist=("plane:coordination",))  # 예외! allowlist에
                           # 실수로 넣어도 hub plane은 못 들어옵니다
```

## 신뢰가 어디에 있나 — 채널이 아니라 신원에

keygen·register-key·init이 뭘 세우는 건지 한 번 정리할게요. "신뢰 가능한 채널을
만드는 절차"라고 생각하기 쉬운데, 정확히는 반대예요 — **채널을 신뢰할 필요가 없게
만드는** 절차입니다.

| 단계 | 세우는 것 |
|---|---|
| `keygen` | **신원** — 위조 불가능한 주장을 할 수 있는 능력(키쌍). 채널과 무관 |
| `register-key` | **신뢰 결정** — "이 공개키가 정말 그 사람이다"를 내 registry에 받아들이는 순간. 신뢰가 개입하는 유일한 지점 |
| `init` | **장부** — 채널이 아니라 내 쪽 원장(수용 기록·영수증·투명 로그) |

전화에 비유하면 구조가 뒤집혀 있어요. 전화선을 도청 불가능하게 만드는 게 아니라,
**목소리를 위조 불가능하게 만들어서 전화선이 뭐든 상관없게** 만듭니다. 봉투는
이메일·채팅·USB 어디로 굴러다녀도 되고, 중간에서 누가 바꾸면 서명이 깨져서 수용이
안 될 뿐이에요.

한국식으로 제일 가까운 비유는 **인감 등록**입니다. 도장을 한 번 등록해두면 그 뒤로는
어떤 문서가 어떤 경로로 오든 인감만 대조하면 진위가 서죠 — 문서를 나른 사람을 신뢰할
필요가 없어요. `register-key`가 그 등록이고, 그래서 **최초 공개키 교환 그 한 번만**
믿을 만한 경로가 필요합니다(직접 전달, 아는 사람 경유). 그 뒤는 신뢰가 아니라 검증이에요.

분명히 해둘 것 하나 — **비밀 유지와는 별개**입니다. 봉투는 서명만 있고 암호화는 없어서
제3자가 *읽는 것*은 막지 않아요. 막는 건 **위조·변조·사후 부인**입니다. 비밀이 필요한
대화가 생기면 그때 sealed 층이 올라갑니다(아래 잔여 목록).

## 경계 — 정직하게

- **위협 모델은 good-faith예요**: 자기 기만과 사후 서사를 막는 도구지, 악의적
  참여자를 막는 게 아니에요. 서명 구현(`schnorr_pure`)은 표준 BIP-340이지만
  상수시간이 아니라서, 적대 환경이면 감사된 라이브러리로 바꿔 끼우세요(형식 호환).
- **배포 경계는 lab-only**: 봉투에 본문을 싣지 않는 전제로 설계됐어요. 공개
  relay·민감 본문 탑재는 이 버전의 승인 범위 밖입니다.
- **명시적 잔여**(숨은 결함이 아니라 다음 범위): capture resolver(값↔bytes 대조),
  sealed 메시지, 저장 동시성, unwind/dispute. key lifecycle("그때 유효했나")은
  0.3.0에서 구현됐어요(`rotate-key`/`revoke-key`/`was-valid`).

## 더 읽기

스펙(봉투 v0.2·wire v1)과 검증 이력(다중 랩 적대적 크리틱·실 relay 라이브 대조 증거)은
개발 저장소에 있고, 공개판은 정리해서 추후 이 매뉴얼 옆에 실어요.
