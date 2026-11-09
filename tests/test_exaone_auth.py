from pathlib import Path

import pytest

from agentcli import AuthProfile, AuthProfileError


def prof(tmp_path, name="svc", **kw):
    return AuthProfile(name, str(tmp_path / name), **kw).prepare()


def test_exaone_environment_uses_profile_key_and_private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAONE_API_KEY", "HOST_KEY")
    monkeypatch.setenv("EXAONE_BASE_URL", "https://host-proxy.example/v1")
    monkeypatch.setenv("EXAONE_HOME", "/host/home")
    p = prof(tmp_path, exaone_api_key=" PROFILE_KEY ")
    env = p.environment("exaone")
    assert env["EXAONE_API_KEY"] == "PROFILE_KEY"
    assert env["EXAONE_HOME"] == str(Path(p.root) / "exaone")
    assert "EXAONE_BASE_URL" not in env
    assert (Path(p.root) / "exaone").is_dir()
    assert p.method("exaone") == "api_key"


def test_exaone_key_is_required(tmp_path):
    with pytest.raises(AuthProfileError):
        prof(tmp_path).environment("exaone")


@pytest.mark.parametrize("name", ["EXAONE_API_KEY", "EXAONE_BASE_URL", "EXAONE_HOME", "exaone_api_key"])
def test_extra_env_cannot_set_exaone_variables(tmp_path, name):
    with pytest.raises(AuthProfileError):
        AuthProfile("svc", str(tmp_path / "x"), extra_env={name: "v"})


def test_exaone_can_be_disabled(tmp_path):
    p = prof(tmp_path, exaone_api_key="K", disabled_providers={"exaone"})
    with pytest.raises(AuthProfileError):
        p.environment("exaone")


def test_exaone_key_never_in_repr_or_public_dict(tmp_path):
    p = prof(tmp_path, exaone_api_key="SECRET_K")
    assert "SECRET_K" not in repr(p)
    assert "SECRET_K" not in str(p.public_dict())


@pytest.mark.parametrize("bad", ["", "  ", "a\nb"])
def test_invalid_exaone_key_rejected(tmp_path, bad):
    with pytest.raises(AuthProfileError):
        AuthProfile("svc", str(tmp_path / "y"), exaone_api_key=bad)


def test_profile_prepared_before_exaone_existed_needs_prepare_again(tmp_path):
    p = prof(tmp_path, exaone_api_key="K")
    (Path(p.root) / "exaone").rmdir()
    with pytest.raises(AuthProfileError):
        p.environment("exaone")
    p.prepare()
    assert p.environment("exaone")["EXAONE_API_KEY"] == "K"


def test_rotation_keeps_exaone_method(tmp_path):
    from dataclasses import replace

    from agentcli import LLMClient, MemoryStore
    from agentcli.providers.registry import ProviderRegistry
    p = prof(tmp_path, exaone_api_key="K1")
    client = LLMClient(MemoryStore(), ProviderRegistry(), auth_profile=p)
    client.set_auth_profile(replace(p, exaone_api_key="K2"))  # same method: allowed
