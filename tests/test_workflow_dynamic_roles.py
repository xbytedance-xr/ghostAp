"""Tests for dynamic roles in workflow script generation prompt.

Validates that the script generation prompt uses dynamic role guidance instead
of a static list of roles, and that the guidance appears in the correct section.
"""

import re
import unittest

from src.workflow_engine.script_gen import build_script_gen_prompt


class TestDynamicRolesGuidance(unittest.TestCase):
    """Test that the prompt contains the dynamic roles guidance text."""

    def _build_prompt(self, requirement="Test requirement"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
        )

    def test_prompt_contains_plan_roles_guidance(self):
        """Test 1: Prompt contains '根据任务需求自行规划角色分工'."""
        prompt = self._build_prompt()
        self.assertIn("根据任务需求自行规划角色分工", prompt)

    def test_prompt_contains_not_fixed_list(self):
        """Test 1: Prompt contains '角色不是固定列表'."""
        prompt = self._build_prompt()
        self.assertIn("角色不是固定列表", prompt)

    def test_prompt_contains_recommended_dimensions(self):
        """Test 1: Prompt contains the recommended dimensions text."""
        prompt = self._build_prompt()
        self.assertIn(
            "建议考虑：架构设计、代码实现、安全审计、正确性验证、测试覆盖等维度",
            prompt,
        )

    def test_all_dynamic_guidance_present(self):
        """Test 1: All three key dynamic guidance phrases are present."""
        prompt = self._build_prompt()
        self.assertIn("根据任务需求自行规划角色分工", prompt)
        self.assertIn("角色不是固定列表", prompt)
        self.assertIn(
            "建议考虑：架构设计、代码实现、安全审计、正确性验证、测试覆盖等维度",
            prompt,
        )


class TestNoStaticRoleList(unittest.TestCase):
    """Test that the prompt does NOT contain a static list of roles."""

    def _build_prompt(self, requirement="Test requirement"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
        )

    def test_no_bullet_architect(self):
        """Test 2: Prompt does not contain '- architect' as a static list item."""
        prompt = self._build_prompt()
        # Match bullet list items that look like static role enumeration
        self.assertNotRegex(
            prompt,
            r"-\s*architect\b",
            "Prompt should not contain '- architect' as a static list item",
        )

    def test_no_bullet_reviewer(self):
        """Test 2: Prompt does not contain '- reviewer' as a static list item."""
        prompt = self._build_prompt()
        self.assertNotRegex(
            prompt,
            r"-\s*reviewer\b",
            "Prompt should not contain '- reviewer' as a static list item",
        )

    def test_no_bullet_tester(self):
        """Test 2: Prompt does not contain '- tester' as a static list item."""
        prompt = self._build_prompt()
        self.assertNotRegex(
            prompt,
            r"-\s*tester\b",
            "Prompt should not contain '- tester' as a static list item",
        )

    def test_no_bullet_coder(self):
        """Test 2: Prompt does not contain '- coder' as a static list item."""
        prompt = self._build_prompt()
        self.assertNotRegex(
            prompt,
            r"-\s*coder\b",
            "Prompt should not contain '- coder' as a static list item",
        )

    def test_no_static_role_enumeration(self):
        """Test 2: No pattern of static role enumeration under Roles section."""
        prompt = self._build_prompt()
        # Find the Roles section
        roles_match = re.search(
            r"\*\*Roles \(specialized perspectives for agents\):\*\*(.*?)(?=\n## |\Z)",
            prompt,
            re.DOTALL,
        )
        self.assertIsNotNone(roles_match, "Roles section should exist")
        roles_section = roles_match.group(1)

        # Check for bullet list of role names (lowercase single words)
        # that would indicate a static list
        static_role_pattern = re.compile(
            r"^\s*-\s*(architect|reviewer|tester|coder|designer)\s*$",
            re.MULTILINE,
        )
        matches = static_role_pattern.findall(roles_section)
        self.assertEqual(
            len(matches),
            0,
            f"Roles section should not contain static role enumeration, found: {matches}",
        )


class TestRolesSectionStructure(unittest.TestCase):
    """Test that dynamic guidance appears under the correct heading."""

    def _build_prompt(self, requirement="Test requirement"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
        )

    def test_roles_heading_exists(self):
        """Test 3: The 'Roles (specialized perspectives for agents)' heading exists."""
        prompt = self._build_prompt()
        self.assertIn(
            "**Roles (specialized perspectives for agents):**",
            prompt,
            "Roles heading should exist in the prompt",
        )

    def test_dynamic_guidance_under_roles_heading(self):
        """Test 3: Dynamic guidance text appears under the Roles heading."""
        prompt = self._build_prompt()

        # Extract the Roles section content
        roles_match = re.search(
            r"\*\*Roles \(specialized perspectives for agents\):\*\*(.*?)(?=\n## |\Z)",
            prompt,
            re.DOTALL,
        )
        self.assertIsNotNone(roles_match, "Roles section should be found")

        roles_content = roles_match.group(1)

        # Verify all dynamic guidance phrases are within the Roles section
        self.assertIn(
            "根据任务需求自行规划角色分工",
            roles_content,
            "Dynamic guidance should be under the Roles heading",
        )
        self.assertIn(
            "角色不是固定列表",
            roles_content,
            "Dynamic guidance should be under the Roles heading",
        )
        self.assertIn(
            "建议考虑：架构设计、代码实现、安全审计、正确性验证、测试覆盖等维度",
            roles_content,
            "Dynamic guidance should be under the Roles heading",
        )

    def test_roles_section_after_tools_section(self):
        """Test 3: Roles section appears after Tools section."""
        prompt = self._build_prompt()

        tools_idx = prompt.find("**Tools (AI agents you can dispatch):**")
        roles_idx = prompt.find("**Roles (specialized perspectives for agents):**")

        self.assertGreater(
            roles_idx,
            tools_idx,
            "Roles section should appear after Tools section",
        )

    def test_roles_section_before_output_format(self):
        """Test 3: Roles section appears before Output Format section."""
        prompt = self._build_prompt()

        roles_idx = prompt.find("**Roles (specialized perspectives for agents):**")
        output_idx = prompt.find("## Output Format")

        self.assertGreater(
            output_idx,
            roles_idx,
            "Roles section should appear before Output Format section",
        )


class TestRoleParameterMention(unittest.TestCase):
    """Test that the guidance mentions the role parameter for agent() calls."""

    def _build_prompt(self, requirement="Test requirement"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
        )

    def test_role_parameter_mentioned(self):
        """Test 4: Prompt mentions the `role` parameter."""
        prompt = self._build_prompt()
        self.assertIn(
            "`role`",
            prompt,
            "Prompt should mention the `role` parameter",
        )

    def test_agent_call_with_role_parameter(self):
        """Test 4: Prompt mentions `role` parameter for `agent()` calls."""
        prompt = self._build_prompt()
        self.assertIn(
            "agent()",
            prompt,
            "Prompt should mention agent() calls",
        )
        # Check that role parameter is mentioned in context of agent() calls
        self.assertIn(
            "每个 agent() 调用可通过 `role` 参数",
            prompt,
            "Prompt should mention role parameter for agent() calls",
        )

    def test_role_examples_given(self):
        """Test 4: Prompt gives role examples like architect、reviewer、tester 等."""
        prompt = self._build_prompt()
        self.assertIn(
            "architect、reviewer、tester 等",
            prompt,
            "Prompt should give role examples",
        )


class TestDynamicWorkflowReliabilityGuidance(unittest.TestCase):
    """Prompt guidance for Claude-style dynamic workflow behavior."""

    def _build_prompt(self, requirement="Diagnose and fix a flaky workflow bug"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["traex", "claude", "aiden"],
            orchestrator_agent="traex",
        )

    def test_prompt_requires_unique_agent_labels(self):
        prompt = self._build_prompt()

        self.assertIn("每个 agent() label 必须唯一", prompt)
        self.assertIn("不要复用 task-analysis", prompt)

    def test_prompt_discourages_slow_monolithic_analysis_agent(self):
        prompt = self._build_prompt()

        self.assertIn("不要先派一个大而慢的 analysis agent", prompt)
        self.assertIn("直接基于用户需求选择 classify/fanout/verify/loop/race", prompt)

    def test_prompt_requires_timeout_and_error_fallbacks(self):
        prompt = self._build_prompt()

        self.assertIn("为每个 agent() 显式设置短超时", prompt)
        self.assertIn("检查 result.error 并提供 fallback", prompt)

    def test_role_examples_in_roles_section(self):
        """Test 4: Role examples appear within the Roles section."""
        prompt = self._build_prompt()

        roles_match = re.search(
            r"\*\*Roles \(specialized perspectives for agents\):\*\*(.*?)(?=\n## |\Z)",
            prompt,
            re.DOTALL,
        )
        self.assertIsNotNone(roles_match)
        roles_content = roles_match.group(1)

        self.assertIn(
            "architect、reviewer、tester 等",
            roles_content,
            "Role examples should be in the Roles section",
        )


class TestDifferentOrchestratorAgents(unittest.TestCase):
    """Test that different orchestrator agents all get dynamic roles guidance."""

    def _build_prompt(self, orchestrator_agent, requirement="Test requirement"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
            orchestrator_agent=orchestrator_agent,
        )

    def test_coco_gets_dynamic_roles(self):
        """Test 5: orchestrator_agent='coco' gets dynamic roles guidance."""
        prompt = self._build_prompt("coco")
        self.assertIn("根据任务需求自行规划角色分工", prompt)
        self.assertIn("角色不是固定列表", prompt)
        self.assertNotRegex(prompt, r"-\s*architect\b")
        self.assertNotRegex(prompt, r"-\s*reviewer\b")

    def test_claude_gets_dynamic_roles(self):
        """Test 5: orchestrator_agent='claude' gets dynamic roles guidance."""
        prompt = self._build_prompt("claude")
        self.assertIn("根据任务需求自行规划角色分工", prompt)
        self.assertIn("角色不是固定列表", prompt)
        self.assertNotRegex(prompt, r"-\s*architect\b")
        self.assertNotRegex(prompt, r"-\s*reviewer\b")

    def test_aiden_gets_dynamic_roles(self):
        """Test 5: orchestrator_agent='aiden' gets dynamic roles guidance."""
        prompt = self._build_prompt("aiden")
        self.assertIn("根据任务需求自行规划角色分工", prompt)
        self.assertIn("角色不是固定列表", prompt)
        self.assertNotRegex(prompt, r"-\s*architect\b")
        self.assertNotRegex(prompt, r"-\s*reviewer\b")

    def test_all_agents_get_recommended_dimensions(self):
        """Test 5: All orchestrator agents get the recommended dimensions."""
        for agent in ["coco", "claude", "aiden"]:
            with self.subTest(orchestrator_agent=agent):
                prompt = self._build_prompt(agent)
                self.assertIn(
                    "建议考虑：架构设计、代码实现、安全审计、正确性验证、测试覆盖等维度",
                    prompt,
                    f"Agent '{agent}' should get recommended dimensions",
                )


class TestDifferentRequirementTypes(unittest.TestCase):
    """Test that different requirement types all get dynamic roles guidance."""

    def _build_prompt(self, requirement):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
        )

    def test_web_app_requirement(self):
        """Test 6: 'Build a web application' gets dynamic roles guidance."""
        prompt = self._build_prompt("Build a web application")
        self.assertIn("根据任务需求自行规划角色分工", prompt)
        self.assertIn("角色不是固定列表", prompt)
        self.assertIn(
            "建议考虑：架构设计、代码实现、安全审计、正确性验证、测试覆盖等维度",
            prompt,
        )

    def test_refactor_requirement(self):
        """Test 6: 'Refactor the authentication module' gets dynamic roles guidance."""
        prompt = self._build_prompt("Refactor the authentication module")
        self.assertIn("根据任务需求自行规划角色分工", prompt)
        self.assertIn("角色不是固定列表", prompt)
        self.assertIn(
            "建议考虑：架构设计、代码实现、安全审计、正确性验证、测试覆盖等维度",
            prompt,
        )

    def test_testing_requirement(self):
        """Test 6: 'Write comprehensive tests' gets dynamic roles guidance."""
        prompt = self._build_prompt("Write comprehensive tests")
        self.assertIn("根据任务需求自行规划角色分工", prompt)
        self.assertIn("角色不是固定列表", prompt)
        self.assertIn(
            "建议考虑：架构设计、代码实现、安全审计、正确性验证、测试覆盖等维度",
            prompt,
        )

    def test_requirement_does_not_affect_roles_section(self):
        """Test 6: Different requirements don't change the roles guidance content."""
        prompts = []
        for req in [
            "Build a web application",
            "Refactor the authentication module",
            "Write comprehensive tests",
        ]:
            prompt = self._build_prompt(req)
            # Extract the roles section
            roles_match = re.search(
                r"\*\*Roles \(specialized perspectives for agents\):\*\*(.*?)(?=\n## |\Z)",
                prompt,
                re.DOTALL,
            )
            self.assertIsNotNone(roles_match)
            prompts.append(roles_match.group(1))

        # All roles sections should be identical
        self.assertEqual(
            prompts[0],
            prompts[1],
            "Roles section should be identical for different requirements",
        )
        self.assertEqual(
            prompts[1],
            prompts[2],
            "Roles section should be identical for different requirements",
        )


class TestNoBudgetContent(unittest.TestCase):
    """Test that the prompt does not contain budget-related content."""

    def _build_prompt(self, requirement="Test requirement"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
        )

    def test_no_budget_english(self):
        """Test 7: Prompt does not contain 'budget' (case insensitive)."""
        prompt = self._build_prompt()
        self.assertNotIn("budget", prompt.lower())

    def test_no_budget_chinese(self):
        """Test 7: Prompt does not contain '预算'."""
        prompt = self._build_prompt()
        self.assertNotIn("预算", prompt)

    def test_no_budget_with_different_agents(self):
        """Test 7: No budget content with different orchestrator agents."""
        for agent in ["coco", "claude", "aiden"]:
            with self.subTest(orchestrator_agent=agent):
                prompt = build_script_gen_prompt(
                    requirement="Test",
                    available_tools=["coco"],
                    orchestrator_agent=agent,
                )
                self.assertNotIn("budget", prompt.lower())
                self.assertNotIn("预算", prompt)


class TestPromptStructure(unittest.TestCase):
    """Test that the prompt has the correct structure with all expected sections."""

    def _build_prompt(self, requirement="Test requirement"):
        return build_script_gen_prompt(
            requirement=requirement,
            available_tools=["coco", "claude", "aiden"],
        )

    def test_has_workflow_script_generation_task(self):
        """Test 8: Prompt has 'Workflow Script Generation Task' section."""
        prompt = self._build_prompt()
        self.assertIn(
            "# Workflow Script Generation Task",
            prompt,
            "Prompt should have main title",
        )

    def test_has_user_requirement_section(self):
        """Test 8: Prompt has 'User Requirement' section."""
        prompt = self._build_prompt()
        self.assertIn(
            "## User Requirement",
            prompt,
            "Prompt should have User Requirement section",
        )

    def test_has_available_resources_section(self):
        """Test 8: Prompt has 'Available Resources' section."""
        prompt = self._build_prompt()
        self.assertIn(
            "## Available Resources",
            prompt,
            "Prompt should have Available Resources section",
        )

    def test_has_tools_section(self):
        """Test 8: Prompt has 'Tools (AI agents you can dispatch)' section."""
        prompt = self._build_prompt()
        self.assertIn(
            "**Tools (AI agents you can dispatch):**",
            prompt,
            "Prompt should have Tools subsection",
        )

    def test_has_roles_section(self):
        """Test 8: Prompt has 'Roles (specialized perspectives for agents)' section."""
        prompt = self._build_prompt()
        self.assertIn(
            "**Roles (specialized perspectives for agents):**",
            prompt,
            "Prompt should have Roles subsection",
        )

    def test_has_output_format_section(self):
        """Test 8: Prompt has 'Output Format' section."""
        prompt = self._build_prompt()
        self.assertIn(
            "## Output Format",
            prompt,
            "Prompt should have Output Format section",
        )

    def test_section_order(self):
        """Test 8: Sections appear in the correct order."""
        prompt = self._build_prompt()

        positions = {
            "title": prompt.find("# Workflow Script Generation Task"),
            "user_requirement": prompt.find("## User Requirement"),
            "available_resources": prompt.find("## Available Resources"),
            "tools": prompt.find("**Tools (AI agents you can dispatch):**"),
            "roles": prompt.find("**Roles (specialized perspectives for agents):**"),
            "output_format": prompt.find("## Output Format"),
        }

        # All sections should be found
        for name, pos in positions.items():
            self.assertGreaterEqual(
                pos,
                0,
                f"Section '{name}' should be found in prompt",
            )

        # Check ordering
        self.assertLess(
            positions["title"],
            positions["user_requirement"],
            "Title should come before User Requirement",
        )
        self.assertLess(
            positions["user_requirement"],
            positions["available_resources"],
            "User Requirement should come before Available Resources",
        )
        self.assertLess(
            positions["available_resources"],
            positions["tools"],
            "Available Resources should come before Tools",
        )
        self.assertLess(
            positions["tools"],
            positions["roles"],
            "Tools should come before Roles",
        )
        self.assertLess(
            positions["roles"],
            positions["output_format"],
            "Roles should come before Output Format",
        )


if __name__ == "__main__":
    unittest.main()
