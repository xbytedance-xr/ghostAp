from src.mode import InteractionMode
from src.utils.engine_identity import resolve_engine_identity


def test_traex_mode_resolves_to_acp_engine_identity():
    identity = resolve_engine_identity(
        mode=InteractionMode.TRAEX,
        acp_tool_name="traex",
        acp_model_name="gpt-5",
    )

    assert identity.engine_name == "Traex"
    assert identity.agent_type == "traex"
    assert identity.model_name == "gpt-5"
    assert identity.transport == "acp"
