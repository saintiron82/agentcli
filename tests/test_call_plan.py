from dataclasses import FrozenInstanceError
import pytest
from agentcli import MemoryStore
from agentcli.planning import CallPlan
from tests.test_turn_state import client_for


def test_prepare_returns_named_frozen_plan():
    client, provider = client_for(MemoryStore())
    plan = client._locked_prepare("task", "gate", "", "", "owner", "system", 3,
                                  "", None, False, False, "alias", None, False)
    assert isinstance(plan, CallPlan)
    assert plan.provider_obj is provider
    assert plan.conv.alias == "alias" and plan.created_conversation
    assert [m.content for m in plan.messages] == ["system", "task"]
    assert plan.resolved_alias == "alias" and not plan.session_id
    with pytest.raises(FrozenInstanceError): plan.model = "changed"
