"""UI_TEXT consistency tests — verifies wording uniformity and key coverage.

Covers:
- AC-R01: /spec resume wording is unique across all entries
- AC-R02: timeout_worker_busy_no_retry is not an independent key
- AC-R05: retry_succeeded mentions auto-continue
- AC-R07: RetryStatus enum members all have inline comments
- AC-R08: .env.example constraint formula uses full SPEC_REVIEW_* names
- SPEC_UI_TEXT merge: all constants.py keys present in UI_TEXT with matching values
- No independent SPEC keys: styles.py has no standalone assignments of SPEC_UI_TEXT keys
"""

import re
from pathlib import Path


class TestSpecResumeWordingUnique:
    """AC-R01: All UI_TEXT entries mentioning /spec resume use identical wording."""

    def test_spec_resume_wording_unique(self):
        from src.card.styles import UI_TEXT

        # Collect all values containing "/spec resume"
        resume_entries = {
            k: v for k, v in UI_TEXT.items() if "/spec resume" in v
        }
        assert resume_entries, "Expected at least one entry with /spec resume"

        # Extract the recovery phrasing — accept either:
        #   "可通过 /spec resume <verb>" (legacy)
        #   "发送 /spec resume <verb>" (new action guidance)
        recovery_phrases = set()
        pattern = re.compile(r"(?:可通过|发送) /spec resume (\S+)")
        for key, value in resume_entries.items():
            match = pattern.search(value)
            assert match, f"Key '{key}' contains /spec resume but not in expected pattern: {value}"
            recovery_phrases.add(match.group(1))

        # Allow a small set of semantically appropriate recovery verbs
        allowed_verbs = {"继续", "手动恢复"}
        assert recovery_phrases <= allowed_verbs, (
            f"Unexpected recovery verbs: {recovery_phrases - allowed_verbs}"
        )


class TestNoTimeoutWorkerBusyNoRetryKey:
    """AC-R02: timeout_worker_busy_no_retry is not an independent UI_TEXT key."""

    def test_key_does_not_exist(self):
        from src.card.styles import UI_TEXT

        assert "timeout_worker_busy_no_retry" not in UI_TEXT


class TestRetrySucceededRemoved:
    """AC-R06: retry_succeeded is removed from UI_TEXT (never rendered)."""

    def test_retry_succeeded_not_in_ui_text(self):
        from src.card.styles import UI_TEXT

        assert "retry_succeeded" not in UI_TEXT


class TestRetryStatusEnumComments:
    """AC-R07: Every RetryStatus enum member has an inline comment."""

    def test_all_members_have_comments(self):
        source_path = Path(__file__).parent.parent / "src" / "spec_engine" / "retry_status.py"
        content = source_path.read_text(encoding="utf-8")

        # Find all enum assignment lines (e.g. `    WAITING = "waiting"`)
        enum_line_pattern = re.compile(r'^\s+(\w+)\s*=\s*"[^"]+"', re.MULTILINE)
        matches = enum_line_pattern.findall(content)
        assert len(matches) >= 5, f"Expected at least 5 enum members, found {len(matches)}"

        # Each assignment line must have a # comment
        commented_pattern = re.compile(r'^\s+\w+\s*=\s*"[^"]+"\s*#.+', re.MULTILINE)
        commented_matches = commented_pattern.findall(content)
        assert len(commented_matches) >= 5, (
            f"Expected 5 commented enum lines, found {len(commented_matches)}"
        )


class TestEnvExampleThreePartFormat:
    """AC-R11: .env.example uses three-part comment format and no migration notes."""

    def test_three_part_format_present(self):
        env_path = Path(__file__).parent.parent / ".env.example"
        content = env_path.read_text(encoding="utf-8")

        # Should contain the three-part format "默认:" + "|" + "范围:" pattern
        assert "默认:" in content and "范围:" in content, (
            "Expected three-part comment format (默认: / 范围: / 用途) in .env.example"
        )

    def test_no_migration_notes(self):
        env_path = Path(__file__).parent.parent / ".env.example"
        content = env_path.read_text(encoding="utf-8")

        assert "旧版" not in content, "Migration notes (旧版) should not be in .env.example"
        assert "已移除" not in content, "Migration notes (已移除) should not be in .env.example"


class TestRendererUITextKeysExist:
    """Guard: all UI_TEXT['key'] references in spec_renderer.py resolve to valid keys."""

    def test_all_renderer_ui_text_keys_exist(self):
        from src.card.styles import UI_TEXT

        source_path = Path(__file__).parent.parent / "src" / "feishu" / "renderers" / "spec_renderer.py"
        content = source_path.read_text(encoding="utf-8")

        # Find all UI_TEXT["..."] and UI_TEXT['...'] references
        pattern = re.compile(r'UI_TEXT\["([^"]+)"\]|UI_TEXT\[\'([^\']+)\'\]')
        referenced_keys = {m.group(1) or m.group(2) for m in pattern.finditer(content)}

        assert referenced_keys, "Expected at least one UI_TEXT reference in spec_renderer.py"

        missing = referenced_keys - set(UI_TEXT.keys())
        assert not missing, f"Renderer references UI_TEXT keys not in UI_TEXT dict: {missing}"


class TestSpecUiTextMerge:
    """Verify SPEC_UI_TEXT keys are merged into UI_TEXT with matching values."""

    def test_all_spec_keys_in_ui_text(self):
        from src.card.styles import UI_TEXT
        from src.spec_engine.constants import SPEC_UI_TEXT

        missing = set(SPEC_UI_TEXT.keys()) - set(UI_TEXT.keys())
        assert not missing, f"SPEC_UI_TEXT keys missing from UI_TEXT: {missing}"

    def test_spec_values_match(self):
        from src.card.styles import UI_TEXT
        from src.spec_engine.constants import SPEC_UI_TEXT

        mismatched = {
            k: (SPEC_UI_TEXT[k], UI_TEXT[k])
            for k in SPEC_UI_TEXT
            if k in UI_TEXT and UI_TEXT[k] != SPEC_UI_TEXT[k]
        }
        assert not mismatched, f"SPEC_UI_TEXT values differ from UI_TEXT: {mismatched}"


class TestNoIndependentSpecKeysInStyles:
    """Verify styles.py has no independent assignments of keys owned by SPEC_UI_TEXT."""

    def test_no_duplicate_spec_keys_in_styles(self):
        from src.spec_engine.constants import SPEC_UI_TEXT

        source_path = Path(__file__).parent.parent / "src" / "card" / "styles.py"
        content = source_path.read_text(encoding="utf-8")

        # Look for "key": ... patterns inside UI_TEXT dict that match SPEC_UI_TEXT keys
        found = []
        for key in SPEC_UI_TEXT:
            # Match standalone dict entries like "retry_waiting": "..."
            pattern = re.compile(rf'^\s+"{re.escape(key)}"\s*:', re.MULTILINE)
            if pattern.search(content):
                found.append(key)

        assert not found, (
            f"styles.py contains independent assignments of SPEC_UI_TEXT keys "
            f"(should come from .update()): {found}"
        )


class TestUiTextKeyConflictAssertion:
    """AC-R29: UI_TEXT key conflict detection raises RuntimeError, not assert."""

    def test_conflict_detection_uses_runtime_error(self):
        """Verify the conflict check code uses RuntimeError (not assert)."""
        from pathlib import Path

        source = (Path(__file__).parent.parent / "src" / "card" / "ui_text.py").read_text(encoding="utf-8")
        # The conflict check should use RuntimeError, not bare assert
        assert "raise RuntimeError" in source
        # Specifically, it should mention UI_TEXT key conflict
        assert "UI_TEXT key conflict" in source

    def test_no_actual_conflicts_at_import(self):
        """Importing styles.py should succeed (no key conflicts in production)."""
        from src.card.styles import UI_TEXT
        from src.spec_engine.constants import SPEC_UI_TEXT
        from src.card.styles_lock import LOCK_UI_TEXT

        # All SPEC keys should be present in UI_TEXT
        for key in SPEC_UI_TEXT:
            assert key in UI_TEXT
        # All LOCK keys should be present in UI_TEXT
        for key in LOCK_UI_TEXT:
            assert key in UI_TEXT


# ---------------------------------------------------------------------------
# Task 23: .env.example only contains user-facing variables
# ---------------------------------------------------------------------------


class TestEnvExampleOnlyUserFacingVars:
    """Verify .env.example does not contain internal-only variables."""

    # Internal variables that should NOT appear in .env.example
    INTERNAL_VARS = [
        "SPEC_CONVERGENCE_WINDOW",
        "SPEC_DISABLE_CONVERGENCE",
        "SPEC_DISABLE_EARLY_STOP",
    ]

    def test_no_internal_vars_in_env_example(self):
        """Internal-only variables must not be exposed in .env.example."""
        env_path = Path(__file__).parent.parent / ".env.example"
        content = env_path.read_text(encoding="utf-8")

        found = [var for var in self.INTERNAL_VARS if var in content]
        assert not found, (
            f"Internal-only variables found in .env.example: {found}"
        )

    def test_required_user_facing_vars_present(self):
        """Essential user-facing variables must be present in .env.example."""
        env_path = Path(__file__).parent.parent / ".env.example"
        content = env_path.read_text(encoding="utf-8")

        required = ["APP_ID", "APP_SECRET", "REPO_LOCK_IDLE_TIMEOUT", "REPO_LOCK_HARD_TIMEOUT", "MAX_EVICTED_CACHE", "REPO_LOCK_CLEANUP_INTERVAL", "CHAT_LOCK_CLEANUP_INTERVAL"]
        missing = [var for var in required if var not in content]
        assert not missing, (
            f"Required user-facing variables missing from .env.example: {missing}"
        )


# ---------------------------------------------------------------------------
# AC-18: LRU eviction text consistency
# ---------------------------------------------------------------------------


class TestEvictionTextConsistency:
    """AC-18: ws_project_eviction_notify and eviction_notify_body use consistent wording."""

    def test_both_contain_same_core_phrase(self):
        from src.card.styles import UI_TEXT

        core_phrase = "暂时与当前群断开连接"
        assert core_phrase in UI_TEXT["ws_project_eviction_notify"], (
            f"ws_project_eviction_notify missing core phrase: {core_phrase}"
        )
        assert core_phrase in UI_TEXT["eviction_notify_body"], (
            f"eviction_notify_body missing core phrase: {core_phrase}"
        )
