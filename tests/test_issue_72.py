"""issue #72 — 출력 스키마 보장: output_schema 옵트인 검증 + 교정 재시도.

부품의 출력 계약: ``chat(payload, output_schema=X)`` 는 "X 모양의
``resp.parsed`` 이거나, ``error_type='schema'`` 로 명명된 실패이거나"다.
검증기는 zero-dep 부분집합(type/required/properties/items/enum)이며, 부분집합
밖 키는 조용한 미검증을 막기 위해 즉시 거부한다.
"""

import pytest

from agentcli import LLMClient, ProviderRegistry
from agentcli.providers.base import LLMProvider
from agentcli.schema import assert_supported_schema, parse_json_output, validate
from agentcli.store.memory import MemoryStore
from agentcli.types import LLMResponse, TokenUsage


# ---- 부분집합 검증기 단위 ----

def test_validate_type_and_required_and_nested():
    schema = {"type": "object", "required": ["results"],
              "properties": {"results": {"type": "array",
                                         "items": {"type": "object",
                                                   "required": ["id"]}}}}
    assert validate({"results": [{"id": "a"}]}, schema) == []
    errs = validate({"results": [{"nope": 1}]}, schema)
    assert any("$.results[0].id" in e for e in errs)
    errs = validate({"results": "not-a-list"}, schema)
    assert any("$.results" in e and "array" in e for e in errs)
    assert any("$.results" in e for e in validate({}, schema))


def test_validate_enum_and_bool_is_not_integer():
    assert validate("b", {"enum": ["a", "b"]}) == []
    assert validate("c", {"enum": ["a", "b"]}) != []
    # bool 은 int 서브클래스 — integer 스키마에 True 가 통과하면 안 된다
    assert validate(True, {"type": "integer"}) != []
    assert validate(3, {"type": "integer"}) == []


def test_unsupported_schema_keys_rejected_loudly():
    """지원 밖 키를 조용히 무시하면 '검증했다고 믿는 미검증'이 된다 — 즉시 거부."""
    with pytest.raises(ValueError, match="pattern"):
        assert_supported_schema({"type": "string", "pattern": "^a"})
    with pytest.raises(ValueError):
        assert_supported_schema({"type": "object",
                                 "properties": {"x": {"$ref": "#/x"}}})


def test_parse_json_output_strips_fences_and_prose():
    obj, err = parse_json_output('```json\n{"a": 1}\n```')
    assert err == "" and obj == {"a": 1}
    obj, err = parse_json_output('설명입니다.\n{"a": 1}\n끝.')
    assert err == "" and obj == {"a": 1}
    obj, err = parse_json_output("JSON 아님")
    assert obj is None and "파싱 실패" in err


# ---- chat 통합: 스크립트된 provider 로 재시도 흐름 고정 ----

class ScriptedProvider(LLMProvider):
    provider_id = "scripted"
    supports_sessions = False
    stores_history = True

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = []                      # 각 호출의 messages 기록

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        self.calls.append(list(messages))
        content = self._outputs.pop(0)
        return LLMResponse(content=content, provider=self.provider_id,
                           model=model,
                           tokens=TokenUsage(prompt_tokens=100,
                                             completion_tokens=10))

    def list_models(self):
        return [{"id": "", "name": "scripted"}]

    def is_available(self):
        return True


def _client(provider):
    reg = ProviderRegistry()
    reg.register(provider)
    reg.set_fallback_order([provider.provider_id])
    return LLMClient(MemoryStore(), registry=reg)


_SCHEMA = {"type": "object", "required": ["ok"],
           "properties": {"ok": {"type": "boolean"}}}


def test_valid_output_first_try_exposes_parsed():
    p = ScriptedProvider(['{"ok": true}'])
    resp = _client(p).chat("q", provider="scripted", owner="o",
                           output_schema=_SCHEMA)
    assert resp.parsed == {"ok": True}
    assert resp.content == '{"ok": true}'
    assert resp.error == ""


def test_corrective_retry_feeds_violation_back():
    p = ScriptedProvider(['{"nope": 1}', '{"ok": false}'])
    resp = _client(p).chat("q", provider="scripted", owner="o",
                           output_schema=_SCHEMA, schema_retries=1)
    assert resp.parsed == {"ok": False}
    assert len(p.calls) == 2
    # 교정 턴에 위반 경로와 이전 출력이 되먹여진다
    retry_text = " ".join(m.content for m in p.calls[1])
    assert "$.ok" in retry_text and '{"nope": 1}' in retry_text
    # 재시도 usage 가 합산된다 (100+100 / 10+10)
    assert resp.tokens.prompt_tokens == 200
    assert resp.tokens.completion_tokens == 20


def test_exhausted_retries_fail_named_with_raw_preserved():
    p = ScriptedProvider(['까부는 산문', '{"ok": "문자열"}'])
    resp = _client(p).chat("q", provider="scripted", owner="o",
                           output_schema=_SCHEMA, schema_retries=1)
    assert resp.content == ""                    # 저장 원자성 계약 유지
    assert resp.error_type == "schema"
    assert resp.raw_content == '{"ok": "문자열"}'   # 마지막 원문 보존
    assert "$.ok" in resp.error
    assert resp.parsed is None


def test_schema_retries_zero_means_single_attempt():
    p = ScriptedProvider(['산문'])
    resp = _client(p).chat("q", provider="scripted", owner="o",
                           output_schema=_SCHEMA, schema_retries=0)
    assert resp.error_type == "schema" and len(p.calls) == 1


def test_validator_callable_path():
    p = ScriptedProvider(['{"ok": true}', '{"ok": false}'])
    resp = _client(p).chat("q", provider="scripted", owner="o",
                           validator=lambda obj: obj.get("ok") is False,
                           schema_retries=1)
    assert resp.parsed == {"ok": False} and len(p.calls) == 2


def test_schema_block_lands_in_system_prompt():
    """output_schema 를 주면 모델에게 스키마를 선언한다 — system 경로로."""
    p = ScriptedProvider(['{"ok": true}'])
    _client(p).chat("q", provider="scripted", owner="o",
                    output_schema=_SCHEMA)
    first_call = " ".join(m.content for m in p.calls[0])
    assert "OUTPUT JSON SCHEMA" in first_call and '"required"' in first_call


def test_transport_failure_passes_through_untouched():
    """전송 실패(content 없음)는 schema 실패로 둔갑하면 안 된다."""
    class FailingProvider(ScriptedProvider):
        def invoke(self, messages, **kw):
            self.calls.append(list(messages))
            return LLMResponse(content="", provider=self.provider_id, model="",
                               tokens=TokenUsage(), error="boom",
                               error_type="timeout")
    p = FailingProvider([])
    resp = _client(p).chat("q", provider="scripted", owner="o",
                           output_schema=_SCHEMA, schema_retries=2)
    assert resp.error_type == "timeout" and len(p.calls) == 1


def test_chat_stream_rejects_output_schema():
    p = ScriptedProvider([])
    client = _client(p)
    with pytest.raises(ValueError, match="스트리밍"):
        client.chat_stream("q", provider="scripted", owner="o",
                           output_schema=_SCHEMA)
