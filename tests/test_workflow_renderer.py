"""Tests for WorkflowProgressRenderer column_set layout (Task 10/16).

Validates:
- render_progress_card produces valid card structure
- Phase sections use column_set elements
- Budget section uses column_set
- render_compact_status produces expected format
- Pagination works for large agent lists
"""

import time
import unittest

from src.workflow_engine.models import (
    AgentProgress,
    AgentStatus,
    BudgetState,
    PhaseProgress,
    WorkflowProject,
    WorkflowStatus,
)
from src.workflow_engine.renderer import (
    WorkflowProgressRenderer,
    _column_set,
    _format_duration,
    _format_tokens,
    _pct,
    render_completion_card,
    render_script_preview,
)


class TestHelperFunctions(unittest.TestCase):
    """Test renderer helper functions."""

    def test_format_duration_seconds(self):
        self.assertEqual(_format_duration(30), "30s")

    def test_format_duration_minutes(self):
        self.assertEqual(_format_duration(90), "1m30s")

    def test_format_duration_hours(self):
        self.assertEqual(_format_duration(3700), "1h1m")

    def test_format_duration_sub_second(self):
        self.assertEqual(_format_duration(0.5), "<1s")

    def test_format_tokens_small(self):
        self.assertEqual(_format_tokens(500), "500")

    def test_format_tokens_k(self):
        self.assertEqual(_format_tokens(5000), "5K")

    def test_format_tokens_m(self):
        self.assertEqual(_format_tokens(2_500_000), "2.5M")

    def test_pct_empty(self):
        pct = _pct(0, 100)
        self.assertIn("0%", pct)

    def test_pct_full(self):
        pct = _pct(100, 100)
        self.assertIn("100%", pct)

    def test_pct_zero_total(self):
        pct = _pct(0, 0)
        self.assertIn("0%", pct)

    def test_column_set_structure(self):
        cs = _column_set([{"tag": "column"}], flex_mode="bisect")
        self.assertEqual(cs["tag"], "column_set")
        self.assertEqual(cs["flex_mode"], "bisect")
        self.assertEqual(len(cs["columns"]), 1)


class TestWorkflowProgressRenderer(unittest.TestCase):
    """Test the full renderer output structure."""

    def _make_project(self, n_agents=3):
        """Create a WorkflowProject with some progress."""
        project = WorkflowProject(
            name="test-workflow",
            status=WorkflowStatus.RUNNING,
            budget=BudgetState(total=2_000_000, used=500_000),
            started_at=time.time() - 60,
        )
        phase = PhaseProgress(
            title="Code Analysis",
            started_at=time.time() - 60,
        )
        for i in range(n_agents):
            agent = AgentProgress(
                label=f"agent_{i}",
                tool="coco",
                status=AgentStatus.DONE if i < n_agents - 1 else AgentStatus.RUNNING,
                duration_s=5.0 if i < n_agents - 1 else 0.0,
                token_usage=10000,
            )
            phase.agents.append(agent)
        project.phases.append(phase)
        project.metrics.total_agents = n_agents
        project.metrics.completed_agents = n_agents - 1
        return project

    def test_render_progress_card_has_header_and_elements(self):
        project = self._make_project()
        renderer = WorkflowProgressRenderer(project)
        card = renderer.render_progress_card()

        self.assertIn("header", card)
        self.assertIn("elements", card)
        self.assertIsInstance(card["elements"], list)
        self.assertGreater(len(card["elements"]), 0)

    def test_render_progress_card_uses_markdown(self):
        """Phase rendering should produce markdown elements (mobile-friendly)."""
        project = self._make_project()
        renderer = WorkflowProgressRenderer(project)
        card = renderer.render_progress_card()

        # Find markdown elements
        md_elements = [e for e in card["elements"] if e.get("tag") == "markdown"]
        self.assertGreater(len(md_elements), 0, "Expected at least one markdown in card")

    def test_budget_section_uses_markdown(self):
        """Budget section should be a markdown element with token display."""
        project = self._make_project()
        renderer = WorkflowProgressRenderer(project)
        budget = renderer._render_budget_section()

        self.assertEqual(budget["tag"], "markdown")
        self.assertIn("预算", budget["content"])
        self.assertIn("/", budget["content"])
        self.assertIn("%", budget["content"])

    def test_render_compact_status_format(self):
        project = self._make_project()
        renderer = WorkflowProgressRenderer(project)
        status = renderer.render_compact_status()

        self.assertIn("WF:", status)
        self.assertIn("test-workflow", status)
        self.assertIn("Phase", status)
        self.assertIn("agents", status)

    def test_pagination_large_agent_list(self):
        """With >20 agents, pagination notice should appear."""
        project = self._make_project(n_agents=25)
        renderer = WorkflowProgressRenderer(project)
        card = renderer.render_progress_card()

        # Should contain pagination notice like "*... +N agents*"
        md_elements = [e for e in card["elements"] if e.get("tag") == "markdown"]
        md_texts = [e["content"] for e in md_elements]
        has_pagination = any("agents" in t and "..." in t for t in md_texts)
        self.assertTrue(has_pagination, "Expected pagination notice for 25 agents")

    def test_phase_section_uses_collapsible_panels(self):
        """Phase rendering should use collapsible_panel grouped by agent status."""
        project = self._make_project()
        # Ensure at least one agent of each major status
        phase = project.phases[0] if project.phases else None
        renderer = WorkflowProgressRenderer(project)
        card = renderer.render_progress_card()

        # Collect all elements (including nested inside collapsible_panel)
        all_tags = [e.get("tag") for e in card["elements"]]
        # Phase section now wraps agents in collapsible_panel, so it shouldn't have any top-level markdown for those
        self.assertIn("collapsible_panel", all_tags)

    def test_header_template_reflects_status(self):
        project = self._make_project()
        renderer = WorkflowProgressRenderer(project)

        # Running → blue
        header = renderer._render_header()
        self.assertEqual(header["template"], "blue")

        # Completed → green
        project.status = WorkflowStatus.COMPLETED
        header = renderer._render_header()
        self.assertEqual(header["template"], "green")

        # Failed → red
        project.status = WorkflowStatus.FAILED
        header = renderer._render_header()
        self.assertEqual(header["template"], "red")


class TestConfirmCardStructure(unittest.TestCase):
    """Test _build_confirm_card output structure from WorkflowHandler."""

    def _make_handler(self):
        from unittest.mock import MagicMock

        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        return handler

    def _make_meta(self, phases=None, tools=None):
        return {
            "name": "test-workflow",
            "description": "A test workflow",
            "phases": phases or [],
            "tools": tools or ["coco"],
        }

    def _build_card(self, meta=None, requirement="test requirement", is_fallback=False, selected_tools=None):
        handler = self._make_handler()
        return handler._build_confirm_card(
            meta=meta or self._make_meta(),
            requirement=requirement,
            engine_session_key="test-session-key-123",
            chat_id="chat_001",
            project_id="proj_001",
            is_fallback=is_fallback,
            selected_tools=selected_tools,
        )

    def _get_elements(self, card):
        return card["body"]["elements"]

    def test_confirm_card_has_phases_list(self):
        """Card should contain markdown listing all phase titles.

        Phases live inside a collapsible_panel (not a top-level markdown element), so we must
        search inside collapsible_panel elements too.
        """
        phases = [
            {"title": "Analysis", "detail": "Analyze codebase"},
            {"title": "Implementation", "detail": "Write code"},
            {"title": "Testing", "detail": "Run tests"},
        ]
        meta = self._make_meta(phases=phases)
        card = self._build_card(meta=meta)
        elements = self._get_elements(card)

        # Recursively collect markdown text from the card (including inside collapsible_panel)
        def flatten_md(els: list[dict]) -> str:
            out: list[str] = []
            for e in els:
                if e.get("tag") == "markdown":
                    out.append(e.get("content", ""))
                if e.get("tag") == "collapsible_panel":
                    out.append(flatten_md(e.get("elements", [])))
            return "\n".join(out)

        all_text = flatten_md(elements)
        for p in phases:
            self.assertIn(p["title"], all_text)

    def test_confirm_card_has_tool_buttons(self):
        """Card should contain tool toggle buttons (in column_set for Schema 2.0 compliance)."""
        meta = self._make_meta(tools=["coco", "claude"])
        card = self._build_card(meta=meta)
        elements = self._get_elements(card)

        # Collect all buttons from both legacy action containers and Schema 2.0 column_set
        all_buttons = []
        for e in elements:
            if e.get("tag") == "action":
                all_buttons.extend(e.get("actions", []))
            if e.get("tag") == "column_set":
                for col in e.get("columns", []):
                    for col_el in col.get("elements", []):
                        if col_el.get("tag") == "button":
                            all_buttons.append(col_el)
            # Also check inside collapsible_panel for "更多工具" buttons
            if e.get("tag") == "collapsible_panel":
                for inner in e.get("elements", []):
                    if inner.get("tag") == "action":
                        all_buttons.extend(inner.get("actions", []))
                    if inner.get("tag") == "column_set":
                        for col in inner.get("columns", []):
                            for col_el in col.get("elements", []):
                                if col_el.get("tag") == "button":
                                    all_buttons.append(col_el)

        tool_buttons = [
            btn for btn in all_buttons
            if btn.get("value", {}).get("action") == "workflow_select_tool"
        ]
        self.assertGreater(len(tool_buttons), 0, "Expected at least one tool selection button")
        button_tool_names = [btn["value"]["tool_name"] for btn in tool_buttons]
        self.assertIn("coco", button_tool_names)
        self.assertIn("claude", button_tool_names)

    def test_confirm_card_has_confirm_cancel_buttons(self):
        """Card should have confirm and cancel buttons (in column_set for Schema 2.0 compliance)."""
        card = self._build_card()
        elements = self._get_elements(card)

        # Collect all buttons from both legacy action containers and Schema 2.0 column_set
        all_buttons = []
        for e in elements:
            if e.get("tag") == "action":
                all_buttons.extend(e.get("actions", []))
            if e.get("tag") == "column_set":
                for col in e.get("columns", []):
                    for col_el in col.get("elements", []):
                        if col_el.get("tag") == "button":
                            all_buttons.append(col_el)

        button_actions = [btn["value"]["action"] for btn in all_buttons]
        self.assertIn("workflow_confirm_start", button_actions)
        self.assertIn("workflow_cancel", button_actions)

    def test_confirm_card_shows_budget(self):
        """Card should contain a markdown element with Token budget info."""
        card = self._build_card()
        elements = self._get_elements(card)

        budget_md = [
            e for e in elements
            if e.get("tag") == "markdown" and "Token 预算" in e.get("content", "")
        ]
        self.assertGreater(len(budget_md), 0, "Expected a markdown element with Token budget")

    def test_confirm_card_fallback_shows_warning(self):
        """Card should show fallback warning when is_fallback=True."""
        card = self._build_card(is_fallback=True)
        elements = self._get_elements(card)

        # Warning is now a "note" element (not markdown) for visual distinction
        warning_elements = [
            e for e in elements
            if (e.get("tag") == "note" and any(
                "默认模板" in sub.get("content", "")
                for sub in e.get("elements", [])
            )) or (e.get("tag") == "markdown" and "默认模板" in e.get("content", ""))
        ]
        self.assertGreater(len(warning_elements), 0, "Expected a note/markdown element with fallback warning")


class TestRenderScriptPreview(unittest.TestCase):
    """Test render_script_preview truncation and formatting."""

    def test_short_script_complete(self):
        """A script under 80 lines and 2000 chars should be shown in full."""
        script = "const x = 1;\nconsole.log(x);\n"
        result = render_script_preview(script)

        self.assertIn("```javascript", result)
        self.assertIn("const x = 1;", result)
        self.assertIn("console.log(x);", result)
        self.assertNotIn("_(脚本已截断，完整内容将在执行时使用)_", result)

    def test_long_script_truncated_by_lines(self):
        """A script with 100 lines should only show first 80 lines with truncation note."""
        lines = [f"// line {i}" for i in range(100)]
        script = "\n".join(lines)
        result = render_script_preview(script)

        self.assertIn("```javascript", result)
        self.assertIn("// line 0", result)
        self.assertIn("// line 79", result)
        self.assertNotIn("// line 80", result)
        self.assertIn("_(脚本已截断，完整内容将在执行时使用)_", result)

    def test_long_script_truncated_by_chars(self):
        """A script where total chars >2000 but <80 lines should be truncated at 2000 chars."""
        # Each line is ~100 chars, 30 lines = ~3000 chars total > 2000
        lines = [f"var longVariable_{i} = " + "x" * 80 + ";" for i in range(30)]
        script = "\n".join(lines)
        self.assertGreater(len(script), 2000)
        self.assertLess(len(lines), 80)

        result = render_script_preview(script)

        self.assertIn("```javascript", result)
        self.assertIn("_(脚本已截断，完整内容将在执行时使用)_", result)
        # The body inside the fence should be at most 2000 chars
        fence_start = result.index("```javascript\n") + len("```javascript\n")
        fence_end = result.index("\n```")
        body = result[fence_start:fence_end]
        self.assertLessEqual(len(body), 2000)

    def test_empty_script_returns_empty(self):
        """Empty string or whitespace-only string should return empty string."""
        self.assertEqual(render_script_preview(""), "")
        self.assertEqual(render_script_preview("   "), "")
        self.assertEqual(render_script_preview("\n\n"), "")

    def test_custom_limits(self):
        """Passing custom max_lines=5 should truncate at 5 lines."""
        lines = [f"line {i}" for i in range(10)]
        script = "\n".join(lines)
        result = render_script_preview(script, max_lines=5)

        self.assertIn("line 0", result)
        self.assertIn("line 4", result)
        self.assertNotIn("line 5", result)
        self.assertIn("_(脚本已截断，完整内容将在执行时使用)_", result)


class TestRenderCompletionCard(unittest.TestCase):
    """Tests for render_completion_card module-level function."""

    def _make_project(self, status=WorkflowStatus.COMPLETED, **kwargs):
        defaults = {
            "name": "code-audit",
            "requirement": "Audit the repository for security issues",
            "status": status,
            "started_at": time.time() - 120,
            "finished_at": time.time(),
            "budget": BudgetState(used=300_000, total=1_500_000),
            "phases": [
                PhaseProgress(
                    title="Analysis",
                    agents=[
                        AgentProgress(label="scan", tool="coco", status=AgentStatus.DONE, duration_s=10.0),
                        AgentProgress(label="verify", tool="claude", status=AgentStatus.DONE, duration_s=15.0),
                    ],
                ),
            ],
            "result": "Found 3 issues.",
        }
        defaults.update(kwargs)
        return WorkflowProject(**defaults)

    def test_returns_header_and_elements(self):
        project = self._make_project()
        card = render_completion_card(project)
        self.assertIn("header", card)
        self.assertIn("elements", card)
        self.assertIsInstance(card["elements"], list)
        self.assertGreater(len(card["elements"]), 0)

    def test_completed_status_green_header(self):
        project = self._make_project(status=WorkflowStatus.COMPLETED)
        card = render_completion_card(project)
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("完成", card["header"]["title"]["content"])

    def test_failed_status_red_header(self):
        project = self._make_project(status=WorkflowStatus.FAILED, error="timeout")
        card = render_completion_card(project)
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("失败", card["header"]["title"]["content"])

    def test_cancelled_status_grey_header(self):
        project = self._make_project(status=WorkflowStatus.CANCELLED)
        card = render_completion_card(project)
        self.assertEqual(card["header"]["template"], "grey")

    def test_elements_contain_metrics(self):
        project = self._make_project()
        card = render_completion_card(project)

        # Recursively extract all markdown content (including from column_set/columns)
        def extract_markdown(elements):
            content = []
            for e in elements:
                if e.get("tag") == "markdown":
                    content.append(e.get("content", ""))
                elif e.get("tag") == "column_set":
                    for col in e.get("columns", []):
                        content.extend(extract_markdown(col.get("elements", [])))
            return content

        all_content = " ".join(extract_markdown(card["elements"]))
        self.assertIn("agents", all_content)
        self.assertIn("Token", all_content)

    def test_elements_contain_phase_summary(self):
        project = self._make_project()
        card = render_completion_card(project)
        all_content = " ".join(
            e.get("content", "") for e in card["elements"] if e.get("tag") == "markdown"
        )
        self.assertIn("Analysis", all_content)

    def test_elements_contain_result_preview(self):
        project = self._make_project(result="Found 3 issues.")
        card = render_completion_card(project)
        all_content = " ".join(
            e.get("content", "") for e in card["elements"] if e.get("tag") == "markdown"
        )
        self.assertIn("Found 3 issues", all_content)

    def test_failed_shows_error_message(self):
        project = self._make_project(status=WorkflowStatus.FAILED, error="Runtime timeout exceeded")
        card = render_completion_card(project)
        all_content = " ".join(
            e.get("content", "") for e in card["elements"] if e.get("tag") == "markdown"
        )
        self.assertIn("Runtime timeout", all_content)


if __name__ == "__main__":
    unittest.main()
