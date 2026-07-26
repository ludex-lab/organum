"""organum obs_schema — vendored JSON Schema 해석 검증기 (observation/v1 구조 검증).

critic A2 교훈("두 검증기는 통일해야 재발 안 함"): producer(organum-code Zod)의 문법을 consumer가
손-포팅하면 반드시 발산한다(backend grammar가 실제로 발산했음). 해법 = producer가 Zod에서 자동 생성한
**portable Draft 2020-12 projection**(`schemas/organum-code-observation-v1.schema.json`, golden test로
drift 방지)을 그대로 vendoring하고, 여기의 **스키마-해석기**가 실행한다 — 문법의 정본은 하나(Zod→
projection), organum엔 문법 사본이 없다.

지원 기능 = 해당 projection이 실제 사용하는 부분집합만: type·properties·required·
additionalProperties(false|schema)·enum·const·pattern·anyOf·items·maxItems·minLength·maxLength·
minimum·maximum·exclusiveMinimum·propertyNames. `format`은 무시(Draft 2020-12에서 annotation이고
pattern이 이미 강제). cross-field superRefine은 projection에 없음 — observatory의 manual invariant가
담당(계약 문서 명시). stdlib only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "organum-code-observation-v1.schema.json"
_cache: dict = {}


def load_observation_schema() -> dict:
    if "v1" not in _cache:
        _cache["v1"] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _cache["v1"]


def _type_ok(value, t: str) -> bool:
    # JSON 타입 판별 — Python bool은 int의 서브클래스라 integer/number에서 명시 제외
    # (critic A2 계열: True가 count로 새는 것 방지)
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        # finite만 (critic A2.1: json.loads가 NaN/Infinity를 허용하고, NaN은 <비교가 항상
        # False라 minimum 검사마저 통과 — 여기서 type 단계에 차단)
        import math
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return False


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """value를 schema로 검증 — 위반 사유 목록(빈 목록=통과). 미지 키워드는 무시(전방 호환),
    단 이 모듈이 지원하는 키워드 집합은 projection 사용분을 전부 덮는다(위 docstring)."""
    errs: list[str] = []

    if "anyOf" in schema:
        branches = schema["anyOf"]
        branch_errs = []
        for b in branches:
            be = validate(value, b, path)
            if not be:
                return []          # 한 branch라도 통과 → OK
            branch_errs.append(be[0])
        return [f"{path}: anyOf 전 branch 실패 ({branch_errs[0]})"]

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return [f"{path}: type {types} 아님 (실제 {type(value).__name__})"]

    if "const" in schema and value != schema["const"]:
        return [f"{path}: const {schema['const']!r} 아님"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: enum {schema['enum']} 밖 값 {value!r}"]

    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: pattern 불일치 {value!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: minLength {schema['minLength']} 미만")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: maxLength {schema['maxLength']} 초과")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: minimum {schema['minimum']} 미만")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: maximum {schema['maximum']} 초과")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errs.append(f"{path}: exclusiveMinimum {schema['exclusiveMinimum']} 이하")

    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: maxItems {schema['maxItems']} 초과")
        if "items" in schema:
            for i, item in enumerate(value):
                errs.extend(validate(item, schema["items"], f"{path}[{i}]"))

    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                errs.append(f"{path}: required '{req}' 없음")
        addl = schema.get("additionalProperties")
        for k, v in value.items():
            if not isinstance(k, str):
                errs.append(f"{path}: 비-문자열 키")
                continue
            if "propertyNames" in schema:
                errs.extend(validate(k, schema["propertyNames"], f"{path}.<key {k!r}>"))
            if k in props:
                errs.extend(validate(v, props[k], f"{path}.{k}"))
            elif addl is False:
                errs.append(f"{path}: 미지 키 '{k}' (strict)")
            elif isinstance(addl, dict):
                errs.extend(validate(v, addl, f"{path}.{k}"))

    return errs
