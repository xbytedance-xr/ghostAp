"""AC5 verification: every agent() call prompt includes the subagent encouragement paragraph.

This is a focused acceptance-test style module. It mirrors the three prompt
construction paths actually used by the workflow engine and asserts that the
standardised subagent encouragement paragraph is present in the rendered
prompt.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from src.workflow_engine.executor import AgentExecutor
from src.workflow_engine.models import AgentCallParams
from src.workflow_engine.roles import SUBAGENT_ENCOURAGEMENT_PROMPT
from src.workflow_engine.script_gen import (
    SUBAGENT_ENCOURAGEMENT,
    build_script_gen_prompt,
    generate_simple_script,
)


def _make_executor(cwd: str = "/tmp") -> AgentExecutor:
    return AgentExecutor(
        cwd=cwd,
        cancel_event=threading.Event(),
        on_token_usage=None,
        max_workers=1,
    )


def _stub_params(prompt: str = "do some work", role: str = "") -> AgentCallParams:
    """Construct a lightweight AgentCallParams for prompt-building tests."""
    return AgentCallParams(
        prompt=prompt,
        role=role,
        tool="coco",
        model=None,
        schema=None,
        label="test",
        timeout_s=30,
    )


class ExecutorPromptInjectionTests(unittest.TestCase):
    """Agent executor appends the encouragement paragraph by default."""

    def setUp(self):
        self._executors = []

    def tearDown(self):
        for executor in self._executors:
            try:
                executor.shutdown(wait=False)
            except Exception:
                pass
        self._executors.clear()

    def _make_executor(self, cwd: str = "/tmp") -> AgentExecutor:
        executor = AgentExecutor(
            cwd=cwd,
            cancel_event=threading.Event(),
            on_token_usage=None,
            max_workers=1,
        )
        self._executors.append(executor)
        return executor

    def test_build_prompt_ends_with_encouragement(self) -> None:
        executor = self._make_executor()
        params = _stub_params(prompt="build a fibonacci function", role="coder")
        full = executor._build_prompt(params)
        self.assertIn("subagent", full.lower(), msg="prompt must mention subagent")
        self.assertTrue(
            full.rstrip().endswith(SUBAGENT_ENCOURAGEMENT_PROMPT),
            msg="executor prompt must end with SUBAGENT_ENCOURAGEMENT_PROMPT",
        )

    def test_build_prompt_without_role_still_has_encouragement(self) -> None:
        executor = self._make_executor()
        params = _stub_params(prompt="just research something", role="")
        full = executor._build_prompt(params)
        self.assertIn("subagent", full.lower())
        self.assertTrue(full.rstrip().endswith(SUBAGENT_ENCOURAGEMENT_PROMPT))

    def test_build_prompt_switch_false_suppresses_encouragement(self) -> None:
        executor = self._make_executor()
        params = _stub_params(prompt="do X", role="")
        fake_settings = mock.MagicMock()
        fake_settings.workflow_subagent_hint_enabled = False
        with mock.patch("src.config.get_settings", return_value=fake_settings):
            full = executor._build_prompt(params)
        self.assertNotIn(SUBAGENT_ENCOURAGEMENT_PROMPT, full)


class ScriptGenPromptInjectionTests(unittest.TestCase):
    """Script-gen prompt embeds the subagent encouragement paragraph by default."""

    def test_script_gen_prompt_contains_encouragement(self) -> None:
        prompt = build_script_gen_prompt(
            requirement="implement a feature",
            available_tools=["coco", "claude"],
            orchestrator_agent="coco",
        )
        self.assertIn(
            SUBAGENT_ENCOURAGEMENT,
            prompt,
            msg="build_script_gen_prompt must include the SUBAGENT_ENCOURAGEMENT paragraph",
        )
        self.assertIn("Subagent Usage Encouragement", prompt)

    def test_script_gen_prompt_switch_false_suppresses_encouragement(self) -> None:
        fake_settings = mock.MagicMock()
        fake_settings.workflow_subagent_hint_enabled = False
        with mock.patch("src.config.get_settings", return_value=fake_settings):
            prompt = build_script_gen_prompt(
                requirement="do Y",
                available_tools=["coco"],
                orchestrator_agent="coco",
            )
        self.assertNotIn(SUBAGENT_ENCOURAGEMENT, prompt)


class SimpleScriptTemplateInjectionTests(unittest.TestCase):
    """generate_simple_script embeds the encouragement in plan / worker prompts."""

    def test_simple_script_injects_enc_in_plan_and_worker(self) -> None:
        script = generate_simple_script(requirement="build a thing")
        self.assertIn(SUBAGENT_ENCOURAGEMENT, script)
        occurrences = script.count(SUBAGENT_ENCOURAGEMENT)
        self.assertGreaterEqual(
            occurrences,
            2,
            msg=f"expected >= 2 _enc injections in simple script, got {occurrences}",
        )

    def test_simple_script_switch_false_drops_enc(self) -> None:
        fake_settings = mock.MagicMock()
        fake_settings.workflow_subagent_hint_enabled = False
        with mock.patch("src.config.get_settings", return_value=fake_settings):
            script = generate_simple_script(requirement="build a thing")
        self.assertNotIn(SUBAGENT_ENCOURAGEMENT, script)


if __name__ == "__main__":
    unittest.main()
