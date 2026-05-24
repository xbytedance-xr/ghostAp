"""Extended unit tests for TaskChainManager.

Covers:
- find_chain_for_task with keyword matching (plan/review/test keywords)
- find_chain_for_task with no matching content (returns default/first chain)
- Template creation and retrieval via add_template / get_template_by_name
- Chain template with multiple roles
- Empty chain manager returns None for find_chain_for_task
"""

import pytest

from src.slock_engine.task_chain_manager import (
    ChainStep,
    ChainTemplate,
    TaskChainManager,
)


# ---------- Fixtures ----------


@pytest.fixture
def manager_with_multiple_chains() -> TaskChainManager:
    """Manager with multiple chain templates for keyword matching tests."""
    config = (
        "planner->coder->reviewer->tester, "
        "coder->reviewer, "
        "coder->tester"
    )
    return TaskChainManager(chain_config=config)


@pytest.fixture
def manager_single_chain() -> TaskChainManager:
    """Manager with a single chain template."""
    return TaskChainManager(chain_config="coder->reviewer->tester")


@pytest.fixture
def empty_manager() -> TaskChainManager:
    """Manager with no valid templates (empty config)."""
    return TaskChainManager(chain_config="invalid_single_role")


# ---------- Tests: find_chain_for_task with keyword matching ----------


class TestFindChainForTaskKeywordMatching:
    """Test keyword matching logic in find_chain_for_task."""

    def test_plan_keyword_returns_planner_chain(self, manager_with_multiple_chains: TaskChainManager):
        """Keywords containing 'plan' should prefer a chain with 'planner' as first role."""
        result = manager_with_multiple_chains.find_chain_for_task("Please plan the architecture")
        assert result is not None
        assert result.first_role == "planner"
        assert result.name == "planner->coder->reviewer->tester"

    def test_architect_keyword_returns_planner_chain(self, manager_with_multiple_chains: TaskChainManager):
        """'architect' keyword should also match the planner chain."""
        result = manager_with_multiple_chains.find_chain_for_task("architect this module")
        assert result is not None
        assert result.first_role == "planner"

    def test_chinese_plan_keyword(self, manager_with_multiple_chains: TaskChainManager):
        """Chinese keyword '设计' should match planner chain."""
        result = manager_with_multiple_chains.find_chain_for_task("请设计这个功能")
        assert result is not None
        assert result.first_role == "planner"

    def test_review_keyword_returns_reviewer_chain(self, manager_with_multiple_chains: TaskChainManager):
        """Keywords containing 'review' should prefer a chain that includes 'reviewer'."""
        result = manager_with_multiple_chains.find_chain_for_task("Please review the code")
        assert result is not None
        roles = [step.role for step in result.steps]
        assert "reviewer" in roles

    def test_chinese_review_keyword(self, manager_with_multiple_chains: TaskChainManager):
        """Chinese keyword '审查' should match reviewer chain."""
        result = manager_with_multiple_chains.find_chain_for_task("审查这段代码")
        assert result is not None
        roles = [step.role for step in result.steps]
        assert "reviewer" in roles

    def test_chinese_check_keyword(self, manager_with_multiple_chains: TaskChainManager):
        """Chinese keyword '检查' should match reviewer chain."""
        result = manager_with_multiple_chains.find_chain_for_task("检查代码质量")
        assert result is not None
        roles = [step.role for step in result.steps]
        assert "reviewer" in roles

    def test_test_keyword_returns_tester_chain(self, manager_with_multiple_chains: TaskChainManager):
        """Keywords containing 'test' should prefer a chain that includes 'tester'."""
        result = manager_with_multiple_chains.find_chain_for_task("Write tests for this feature")
        assert result is not None
        roles = [step.role for step in result.steps]
        assert "tester" in roles

    def test_chinese_test_keyword(self, manager_with_multiple_chains: TaskChainManager):
        """Chinese keyword '测试' should match tester chain."""
        result = manager_with_multiple_chains.find_chain_for_task("请编写测试用例")
        assert result is not None
        roles = [step.role for step in result.steps]
        assert "tester" in roles

    def test_keyword_matching_is_case_insensitive(self, manager_with_multiple_chains: TaskChainManager):
        """Keyword matching should be case-insensitive."""
        result = manager_with_multiple_chains.find_chain_for_task("PLAN this project")
        assert result is not None
        assert result.first_role == "planner"


# ---------- Tests: find_chain_for_task with no matching content ----------


class TestFindChainForTaskNoMatch:
    """Test default behavior when no keywords match."""

    def test_no_keyword_match_returns_longest_chain(self, manager_with_multiple_chains: TaskChainManager):
        """When no keywords match, find_chain_for_task returns the longest (most comprehensive) chain."""
        result = manager_with_multiple_chains.find_chain_for_task("do something generic")
        assert result is not None
        # The longest chain is "planner->coder->reviewer->tester" (4 steps)
        assert len(result.steps) == 4
        assert result.name == "planner->coder->reviewer->tester"

    def test_empty_task_content_returns_longest_chain(self, manager_with_multiple_chains: TaskChainManager):
        """Empty task content with no role should return the longest chain."""
        result = manager_with_multiple_chains.find_chain_for_task("")
        assert result is not None
        assert len(result.steps) == 4

    def test_unrelated_content_returns_default(self, manager_single_chain: TaskChainManager):
        """Unrelated content with a single-chain manager returns that chain."""
        result = manager_single_chain.find_chain_for_task("hello world random text")
        assert result is not None
        assert result.name == "coder->reviewer->tester"


# ---------- Tests: Template creation and retrieval ----------


class TestTemplateCreationAndRetrieval:
    """Test template creation via config parsing and retrieval via get_template_by_name."""

    def test_templates_parsed_from_config(self, manager_with_multiple_chains: TaskChainManager):
        """Templates should be correctly parsed from comma-separated config."""
        templates = manager_with_multiple_chains.templates
        assert len(templates) == 3

    def test_get_template_by_name_found(self, manager_with_multiple_chains: TaskChainManager):
        """get_template_by_name returns the matching template."""
        result = manager_with_multiple_chains.get_template_by_name("coder->reviewer")
        assert result is not None
        assert result.name == "coder->reviewer"
        assert len(result.steps) == 2

    def test_get_template_by_name_full_chain(self, manager_with_multiple_chains: TaskChainManager):
        """get_template_by_name works for longer chain names."""
        result = manager_with_multiple_chains.get_template_by_name("planner->coder->reviewer->tester")
        assert result is not None
        assert len(result.steps) == 4

    def test_get_template_by_name_not_found(self, manager_with_multiple_chains: TaskChainManager):
        """get_template_by_name returns None for nonexistent names."""
        result = manager_with_multiple_chains.get_template_by_name("nonexistent->chain")
        assert result is None

    def test_add_template_via_direct_append(self):
        """Simulate adding a template by appending to internal list (no public add_template)."""
        manager = TaskChainManager(chain_config="coder->reviewer")
        new_template = ChainTemplate(
            name="writer->editor",
            steps=[ChainStep(role="writer", order=0), ChainStep(role="editor", order=1)],
        )
        manager._templates.append(new_template)

        # Verify retrieval
        result = manager.get_template_by_name("writer->editor")
        assert result is not None
        assert result.first_role == "writer"
        assert result.last_role == "editor"


# ---------- Tests: Chain template with multiple roles ----------


class TestChainTemplateMultipleRoles:
    """Test chain templates with multiple roles for successor/predecessor logic."""

    def test_four_step_chain_roles(self, manager_with_multiple_chains: TaskChainManager):
        """A 4-step chain should have correct ordered roles."""
        template = manager_with_multiple_chains.get_template_by_name("planner->coder->reviewer->tester")
        assert template is not None
        roles = [step.role for step in template.steps]
        assert roles == ["planner", "coder", "reviewer", "tester"]

    def test_successor_in_multi_role_chain(self):
        """Successor traversal should work across a multi-role chain."""
        manager = TaskChainManager(chain_config="planner->coder->reviewer->tester")
        template = manager.templates[0]
        assert template.successor("planner") == "coder"
        assert template.successor("coder") == "reviewer"
        assert template.successor("reviewer") == "tester"
        assert template.successor("tester") is None

    def test_predecessor_in_multi_role_chain(self):
        """Predecessor traversal should work across a multi-role chain."""
        manager = TaskChainManager(chain_config="planner->coder->reviewer->tester")
        template = manager.templates[0]
        assert template.predecessor("tester") == "reviewer"
        assert template.predecessor("reviewer") == "coder"
        assert template.predecessor("coder") == "planner"
        assert template.predecessor("planner") is None

    def test_first_and_last_role_properties(self):
        """first_role and last_role properties should reflect the chain ends."""
        manager = TaskChainManager(chain_config="designer->coder->reviewer->qa->deployer")
        template = manager.templates[0]
        assert template.first_role == "designer"
        assert template.last_role == "deployer"

    def test_find_chain_by_starting_role_picks_longest(self):
        """When matching by starting_role, the longest chain containing that role is returned."""
        manager = TaskChainManager(chain_config="coder->reviewer, planner->coder->reviewer->tester")
        result = manager.find_chain_for_task("", starting_role="coder")
        assert result is not None
        # The longer chain (4 steps) contains "coder" and should be picked
        assert len(result.steps) == 4
        assert result.name == "planner->coder->reviewer->tester"


# ---------- Tests: Empty chain manager ----------


class TestEmptyChainManager:
    """Test behavior when TaskChainManager has no valid templates."""

    def test_find_chain_for_task_returns_none(self, empty_manager: TaskChainManager):
        """With no templates, find_chain_for_task should return None."""
        result = empty_manager.find_chain_for_task("plan something")
        assert result is None

    def test_find_chain_for_task_empty_content_returns_none(self, empty_manager: TaskChainManager):
        """With no templates and empty content, find_chain_for_task should return None."""
        result = empty_manager.find_chain_for_task("")
        assert result is None

    def test_find_chain_for_task_with_role_returns_none(self, empty_manager: TaskChainManager):
        """With no templates, find_chain_for_task with a starting_role still returns None."""
        result = empty_manager.find_chain_for_task("code", starting_role="coder")
        assert result is None

    def test_templates_list_is_empty(self, empty_manager: TaskChainManager):
        """Empty manager should have no templates."""
        assert empty_manager.templates == []

    def test_get_template_by_name_returns_none(self, empty_manager: TaskChainManager):
        """get_template_by_name returns None when no templates exist."""
        assert empty_manager.get_template_by_name("anything") is None
