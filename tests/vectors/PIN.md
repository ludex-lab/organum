# Vendored test vectors — provenance pin

## `bip340-test-vectors.csv`

- **원천**: `https://raw.githubusercontent.com/bitcoin/bips/master/bip-0340/test-vectors.csv`
  (bitcoin/bips, BIP-340 규범 부속물)
- **수집**: 2026-08-26, 원시 바이트 그대로(모델·요약·전사 경유 0)
- **content SHA-256**:
  `34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d`
- **내용**: 19 벡터(index 0–18) — 서명 8건(secret key 동반) + 검증 전용 11건,
  그중 10건이 반드시 **거부**되어야 하는 음성 케이스(곡선 밖 공개키·has_even_y(R)
  false·negated message/s·sG−eP 무한원점·필드 크기 초과 등).

### 왜 벤더링하고 SHA를 박아두나

이 파일은 우리 서명 검증이 옳다고 말하는 **근거**다. 근거는 기억이나 산문에서
오면 안 된다 — 0.4.8 키 사고(축약 지문에서 전체 키를 기억으로 재구성)와 같은
가문이다. 실제로 이 벡터를 깔던 날, 나는 벡터 0의 서명을 기억으로 `…2DBA82…`
라고 떠올렸는데 공식 값은 `…2DCA82…`였다. **기억으로 적었다면 우리 구현이
정확한데도 틀렸다고 판정하는 감사기를 만들 뻔했다.**

갱신 시: 원천을 다시 받아 **SHA를 갱신하는 커밋과 구현 변경 커밋을 섞지 말 것**
(벡터가 바뀐 것인지 구현이 바뀐 것인지 사후에 갈라낼 수 없게 된다).
