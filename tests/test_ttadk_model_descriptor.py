def test_resolve_model_id_maps_display_and_alias_to_model_id():
    from src.ttadk.models import ModelDescriptor, resolve_model_id

    desc = [
        ModelDescriptor(
            model_id="gpt-5.2-codex-ttadk",
            display_name="GPT 5.2 Codex",
            aliases=["gpt-5.2", "GPT 5.2"],
            source="probe",
            verified=True,
        ),
        ModelDescriptor(model_id="glm-5-ttadk", display_name="GLM 5", aliases=["glm-5"], source="probe", verified=True),
    ]

    r1, d1 = resolve_model_id(tool_name="codex", input_name="GPT 5.2 Codex", descriptors=desc)
    assert r1.real_name == "gpt-5.2-codex-ttadk"
    assert r1.source in ("friendly", "exact")
    assert (d1 or {}).get("resolution_source") in ("friendly", "exact")

    r2, _ = resolve_model_id(tool_name="codex", input_name="gpt-5.2", descriptors=desc)
    assert r2.real_name == "gpt-5.2-codex-ttadk"


def test_resolve_model_id_unknown_returns_candidates_without_passthrough():
    from src.ttadk.models import ModelDescriptor, resolve_model_id

    desc = [
        ModelDescriptor(
            model_id="gpt-5.2-codex-ttadk",
            display_name="GPT 5.2 Codex",
            aliases=["gpt-5.2"],
            source="probe",
            verified=True,
        ),
        ModelDescriptor(
            model_id="gpt-4.1-ttadk", display_name="GPT 4.1", aliases=["gpt-4"], source="probe", verified=True
        ),
    ]

    r, d = resolve_model_id(tool_name="codex", input_name="gpt", descriptors=desc)
    assert r.source in ("prefix", "partial", "unknown")

    # 输入为明显 display/短词且无精确命中时，不应进入 token passthrough
    if r.source == "unknown":
        assert "unknown_model_input" in (r.warnings or [])
        assert isinstance((d or {}).get("candidates"), list)
