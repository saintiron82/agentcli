from unittest.mock import patch
from agentcli.providers.kiro import KiroProvider


def test_provider_id_and_capabilities():
    p = KiroProvider()
    assert p.provider_id == "kiro"
    assert p.supports_sessions is True
    assert p.supports_streaming is True
    assert p.stores_history is False


def test_list_models_has_default_passthrough():
    models = KiroProvider().list_models()
    assert any(m["id"] == "" for m in models)  # 빈 id = 기본
    # resolve_model 은 알 수 없는 selector 를 그대로 통과 (비-strict).
    assert KiroProvider().resolve_model("kiro-some-model") == "kiro-some-model"


@patch("agentcli.providers.kiro.shutil.which", return_value=None)
def test_health_check_binary_missing(mock_which):
    h = KiroProvider().health_check()
    assert h.ok is False
    assert h.status == "binary_missing"
    assert h.error_type == "binary_missing"
