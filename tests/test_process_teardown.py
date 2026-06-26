"""프로세스 그룹 teardown 의 분기/가드 단위 테스트 (OS 무관 — monkeypatch).

``test_process_group.py`` 가 실제 손자 reap(POSIX)을 검증한다면, 여기서는
``_kill_process_group``/``_new_session_kwargs`` 의 POSIX vs Windows 분기와
race(ProcessLookupError) 폴백을 결정적으로 검증한다.
"""
import signal
from unittest.mock import MagicMock

import agentcli.providers.base as base


def test_new_session_kwargs_posix_vs_windows(monkeypatch):
    monkeypatch.setattr(base, "_POSIX", True)
    assert base._new_session_kwargs() == {"start_new_session": True}
    monkeypatch.setattr(base, "_POSIX", False)
    assert base._new_session_kwargs() == {}


def test_kill_process_group_posix_uses_killpg(monkeypatch):
    """POSIX: proc.pid 를 PGID 로 killpg, 성공 시 proc.kill 안 부른다."""
    monkeypatch.setattr(base, "_POSIX", True)
    calls = []
    monkeypatch.setattr(base.os, "killpg", lambda pg, sig: calls.append((pg, sig)))
    proc = MagicMock(); proc.pid = 4321
    base._kill_process_group(proc)
    assert calls == [(4321, signal.SIGKILL)]
    proc.kill.assert_not_called()


def test_kill_process_group_windows_falls_back_to_direct_kill(monkeypatch):
    """비-POSIX: killpg 호출 안 하고 직속 proc.kill 만."""
    monkeypatch.setattr(base, "_POSIX", False)
    killpg = MagicMock()
    monkeypatch.setattr(base.os, "killpg", killpg)
    proc = MagicMock(); proc.pid = 123
    base._kill_process_group(proc)
    killpg.assert_not_called()
    proc.kill.assert_called_once()


def test_kill_process_group_lookup_error_falls_back(monkeypatch):
    """그룹이 이미 사라진(race) ProcessLookupError 면 폴백, 예외 전파 안 함."""
    monkeypatch.setattr(base, "_POSIX", True)

    def boom(pg, sig):
        raise ProcessLookupError

    monkeypatch.setattr(base.os, "killpg", boom)
    proc = MagicMock(); proc.pid = 9
    base._kill_process_group(proc)  # raise 없어야 함
    proc.kill.assert_called_once()


def test_kill_process_group_no_pid_is_safe(monkeypatch):
    """pid 없는(proc 미생성) 경우에도 죽지 않는다."""
    monkeypatch.setattr(base, "_POSIX", True)
    monkeypatch.setattr(base.os, "killpg", MagicMock(side_effect=AssertionError("불러선 안 됨")))
    proc = MagicMock(); proc.pid = None
    base._kill_process_group(proc)
    proc.kill.assert_called_once()
