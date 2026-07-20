from src.ttadk.models import ModelDescriptor


def test_model_resolver_resolves_aliases_and_exposes_diagnostics():
    from src.ttadk.model_resolver import resolve_model_id

    resolved, diagnostics = resolve_model_id(
        tool_name="codex",
        input_name="GPT 5.2",
        descriptors=[
            ModelDescriptor(
                model_id="gpt-5.2-codex-ttadk",
                display_name="GPT 5.2 Codex",
                aliases=["GPT 5.2"],
                source="probe",
                verified=True,
            )
        ],
    )

    assert resolved.real_name == "gpt-5.2-codex-ttadk"
    assert resolved.source == "friendly"
    assert resolved.validated is True
    assert diagnostics["model_display"] == "GPT 5.2 Codex"
    assert diagnostics["resolution_reason"] == "friendly_or_alias_hit"


def test_model_resolver_chooses_best_available_model_fallback():
    from src.ttadk.model_resolver import choose_best_available_model

    assert choose_best_available_model(
        input_model="gpt-5.2",
        available_models=["gpt-4.1-codex-ttadk", "gpt-5.2-codex-ttadk"],
    ) == "gpt-5.2-codex-ttadk"


def test_models_resolve_model_id_delegates_to_model_resolver(monkeypatch):
    from src.ttadk import models

    calls = []

    def fake_resolve_model_id(**kwargs):
        calls.append(kwargs)
        return (
            models.ResolvedModelResult(
                tool_name=kwargs["tool_name"],
                input_name=kwargs["input_name"],
                real_name="resolved-by-resolver",
                source="friendly",
                validated=True,
            ),
            {"resolution_source": "friendly"},
        )

    monkeypatch.setattr(models._model_resolver, "resolve_model_id", fake_resolve_model_id)

    result, diagnostics = models.resolve_model_id(
        tool_name="codex",
        input_name="GPT 5.2",
        descriptors=[ModelDescriptor(model_id="gpt-5.2-codex-ttadk", aliases=["GPT 5.2"])],
    )

    assert result.real_name == "resolved-by-resolver"
    assert diagnostics == {"resolution_source": "friendly"}
    assert calls[0]["tool_name"] == "codex"
    assert calls[0]["input_name"] == "GPT 5.2"


def test_model_fetching_result_types_are_reexported_from_public_fetcher_api():
    """Task 31: model_fetcher keeps public API while result dataclasses live in helper module."""
    from src.ttadk import model_fetcher
    from src.ttadk.model_fetching import FetchDiagnostics, FetchResult, TTADKRunResult

    assert model_fetcher.FetchDiagnostics is FetchDiagnostics
    assert model_fetcher.FetchResult is FetchResult
    assert model_fetcher.TTADKRunResult is TTADKRunResult

    diag = FetchDiagnostics(tool_name="codex")
    result = FetchResult(tool_name="codex", diagnostics=diag)
    run_result = TTADKRunResult(returncode=0, stdout="ok", stderr="")

    assert result.diagnostics.tool_name == "codex"
    assert run_result.stdout == "ok"
