from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESTART_SCRIPT = ROOT / "restart.sh"


def test_restart_script_preheats_codex_acp_fallback_dependency():
    text = RESTART_SCRIPT.read_text(encoding="utf-8")

    assert "CODEX_ACP_NPM_PACKAGE=" in text
    assert "@agentclientprotocol/codex-acp@1.1.2" in text
    assert "PREPARE_CODEX_ACP=" in text
    assert "prepare_codex_acp_dependency()" in text
    assert 'npx --yes "$CODEX_ACP_NPM_PACKAGE" --version' in text
    assert 'npx --yes "$CODEX_ACP_NPM_PACKAGE" --help' not in text
    assert "prepare_codex_acp_dependency" in text.split("start_service() {", 1)[1]


def test_restart_script_syncs_python_and_prepares_platform_sandbox():
    text = RESTART_SCRIPT.read_text(encoding="utf-8")
    start_body = text.split("start_service() {", 1)[1]

    assert "GHOSTAP_SYNC_PYTHON_DEPENDENCIES" in text
    assert "uv sync --check --group dev" in text
    assert "uv sync --group dev" in text
    assert "prepare_python_dependencies || exit 1" in start_body
    assert "GHOSTAP_PREPARE_EMPLOYEE_SANDBOX" in text
    assert "prepare_employee_sandbox_dependency" in start_body
    assert "apt-get install -y bubblewrap" in text
    assert "dnf install -y bubblewrap" in text
    assert "pacman -S --needed --noconfirm bubblewrap" in text
    assert "pacman -Sy" not in text
    assert "/usr/bin/sandbox-exec" in text
    assert "mechanism=seatbelt" in text


def test_restart_script_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", str(RESTART_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
