from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESTART_SCRIPT = ROOT / "restart.sh"


def test_restart_script_preheats_codex_acp_fallback_dependency():
    text = RESTART_SCRIPT.read_text(encoding="utf-8")

    assert "CODEX_ACP_NPM_PACKAGE=" in text
    assert "@zed-industries/codex-acp@0.14.0" in text
    assert "PREPARE_CODEX_ACP=" in text
    assert "prepare_codex_acp_dependency()" in text
    assert 'npx --yes "$CODEX_ACP_NPM_PACKAGE" --help' in text
    assert "prepare_codex_acp_dependency" in text.split("start_service() {", 1)[1]


def test_restart_script_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", str(RESTART_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
