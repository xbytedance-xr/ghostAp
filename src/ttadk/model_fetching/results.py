"""Pure data containers used by TTADK model fetching."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import TTADKModel


@dataclass
class FetchDiagnostics:
    tool_name: str
    attempts: list[dict] = field(default_factory=list)
    chosen_strategy: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    tool_name: str
    models: list[TTADKModel] = field(default_factory=list)
    source: str = ""
    diagnostics: FetchDiagnostics = field(default_factory=lambda: FetchDiagnostics(tool_name=""))

    def __post_init__(self) -> None:
        if not getattr(self.diagnostics, "tool_name", None):
            self.diagnostics.tool_name = self.tool_name


@dataclass
class TTADKRunResult:
    returncode: int
    stdout: str
    stderr: str

