"""출력 스키마 보장 (#72) — zero-dep JSON Schema 부분집합 검증 + 출력 파싱.

"부품"의 출력 계약을 담당한다: 모델 텍스트 → (펜스 제거) → JSON 파싱 →
부분집합 검증. 부분집합만 지원한다: ``type`` / ``required`` /
``properties`` / ``items`` / ``enum``. 그 밖의 키($ref, pattern, anyOf …)를
조용히 무시하면 "검증했다고 믿는데 안 한" 상태가 되므로, 스키마 수리 시점에
``assert_supported_schema`` 가 ValueError 로 즉시 거부한다. 런타임 의존성 0
불변식 때문에 jsonschema 라이브러리는 쓰지 않는다 — 더 넓은 검증이 필요하면
``validator=`` (호출자 callable) 가 탈출구다.
"""

import json
import re

_SUPPORTED_KEYS = frozenset({"type", "required", "properties", "items", "enum"})
_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}

# ```json ... ``` 또는 ``` ... ``` 전체 펜스 — 모델이 지시를 어기고 감싸는
# 가장 흔한 형태만 벗긴다 (부분 펜스/중첩은 substring 폴백이 받는다).
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.DOTALL)


def assert_supported_schema(schema, path: str = "$") -> None:
    """스키마가 지원 부분집합 안인지 수리 시점 검사 — 밖이면 ValueError.

    조용한 미검증 방지가 목적이므로, 호출 전에(재시도 비용을 쓰기 전에)
    터뜨린다.
    """
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: 스키마 노드는 dict 여야 한다 "
                         f"({type(schema).__name__})")
    unknown = sorted(set(schema) - _SUPPORTED_KEYS)
    if unknown:
        raise ValueError(
            f"{path}: 지원하지 않는 스키마 키 {unknown} — 지원 부분집합은 "
            f"{sorted(_SUPPORTED_KEYS)} 뿐이다. 더 넓은 검증은 validator= "
            f"callable 로.")
    t = schema.get("type")
    if t is not None and t not in _TYPE_MAP:
        raise ValueError(f"{path}.type: 알 수 없는 타입 {t!r} "
                         f"(지원: {sorted(_TYPE_MAP)})")
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        raise ValueError(f"{path}.properties: dict 여야 한다")
    for name, sub in props.items():
        assert_supported_schema(sub, f"{path}.{name}")
    if "items" in schema:
        assert_supported_schema(schema["items"], f"{path}[]")


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    """위반 목록을 반환한다 (빈 리스트 = 통과). 경로는 ``$.a[0].b`` 형식.

    위반 문구는 교정 재시도 프롬프트에 그대로 되먹여지므로, 모델이 고칠 수
    있게 "어디가·무엇이어야 하는데·무엇이었다" 를 담는다.
    """
    errs: list[str] = []
    t = schema.get("type")
    if t is not None:
        # bool 은 int 의 서브클래스 — integer/number 에 True 가 통과하지 않게.
        if isinstance(instance, bool) and t in ("integer", "number"):
            return [f"{path}: {t} 이어야 하는데 boolean"]
        if not isinstance(instance, _TYPE_MAP[t]):
            return [f"{path}: {t} 이어야 하는데 {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path}: 허용값 {schema['enum']} 밖의 {instance!r}")
    if isinstance(instance, dict):
        for req in schema.get("required", ()):
            if req not in instance:
                errs.append(f"{path}.{req}: 필수 필드 누락")
        for name, sub in (schema.get("properties") or {}).items():
            if name in instance:
                errs.extend(validate(instance[name], sub, f"{path}.{name}"))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errs.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errs


def parse_json_output(text: str):
    """모델 출력 → ``(객체, "")`` 또는 ``(None, 오류메시지)``.

    1) 전체를 감싼 마크다운 펜스를 벗기고 그대로 파싱을 시도한다.
    2) 실패하면 첫 ``{``/``[`` 부터 마지막 ``}``/``]`` 구간을 한 번 더
       시도한다 — 모델이 JSON 앞뒤에 산문을 붙이는 가장 흔한 위반의 회수.
    """
    s = (text or "").strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s), ""
    except json.JSONDecodeError as exc:
        starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
        end = max(s.rfind("}"), s.rfind("]"))
        if starts and end > min(starts):
            try:
                return json.loads(s[min(starts):end + 1]), ""
            except json.JSONDecodeError:
                pass
        return None, f"JSON 파싱 실패: {exc.msg} (pos {exc.pos})"
