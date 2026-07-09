"""Full Workflow execution report generation.

The Feishu card is a compact status surface; this module writes the full,
untruncated execution payload to local report files that can be sent as an IM
attachment.
"""

from __future__ import annotations

import html
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AgentStatus, WorkflowProject

DEFAULT_WORKFLOW_CACHE_ROOT = "~/.cache/ghostAp"
_REPORT_DIR = "workflow_reports"


@dataclass(frozen=True)
class WorkflowReportFiles:
    """Paths for a generated Workflow report bundle."""

    run_id: str
    html_path: str
    markdown_path: str
    html_filename: str
    markdown_filename: str


@dataclass(frozen=True)
class _ReportSection:
    title: str
    body: str
    expanded: bool = False


def _safe_slug(value: str, *, default: str = "workflow") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return slug[:80] or default


def workflow_cache_root(cache_root: str | None = None) -> str:
    """Return the local cache root shared by Workflow runtime artifacts."""
    root = cache_root or DEFAULT_WORKFLOW_CACHE_ROOT
    return os.path.abspath(os.path.expanduser(root))


def workflow_project_cache_root(root_path: str, cache_root: str | None = None) -> str:
    """Mirror an absolute project path under ``~/.cache/ghostAp``."""
    abs_project = os.path.abspath(os.path.expanduser(root_path or "."))
    drive, tail = os.path.splitdrive(abs_project)
    parts = [part for part in Path(tail).parts if part not in (os.sep, "")]
    if drive:
        parts.insert(0, drive.rstrip(":"))
    return os.path.join(workflow_cache_root(cache_root), *parts)


def _status_text(value: Any) -> str:
    return getattr(value, "value", str(value or ""))


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h{mins}m"


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _parse_json_payload(raw_result: str | None) -> Any | None:
    text = str(raw_result or "").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _format_full_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return _pretty_json(value)


def _humanize_key(key: str) -> str:
    labels = {
        "summary": "摘要",
        "final_report": "最终报告",
        "report": "报告",
        "result": "结果",
        "output": "输出",
        "status": "状态",
        "conclusion": "结论",
        "verification": "验证",
        "reviews": "评审",
        "risks": "风险",
        "risk": "风险",
        "findings": "发现",
        "recommendations": "建议",
        "next_steps": "后续",
        "worker_findings": "执行发现",
        "parallel_results": "并行结果",
        "results": "结果",
        "agent_outputs": "Agent 原始输出",
        "raw_outputs": "原始输出",
        "logs": "日志",
    }
    return labels.get(key, key.replace("_", " "))


def _workflow_summary(project: WorkflowProject) -> str:
    metrics = project.metrics
    phase_agents = [agent for phase in project.phases for agent in phase.agents]
    total_agents = metrics.total_agents or len(phase_agents)
    completed_agents = metrics.completed_agents or sum(
        1 for agent in phase_agents if agent.status in (AgentStatus.DONE, AgentStatus.CACHED)
    )
    failed_agents = metrics.failed_agents or sum(1 for agent in phase_agents if agent.status == AgentStatus.FAILED)
    cached_agents = metrics.cached_agents or sum(1 for agent in phase_agents if agent.status == AgentStatus.CACHED)
    total_tokens = metrics.total_tokens or sum(agent.token_usage for agent in phase_agents)

    elapsed = 0.0
    if project.started_at:
        end_time = project.finished_at or datetime.now(timezone.utc).timestamp()
        elapsed = max(0.0, end_time - project.started_at)

    lines = [
        f"- 任务: {project.requirement or project.name or 'Workflow'}",
        f"- 名称: {project.name or 'Workflow'}",
        f"- 状态: {_status_text(project.status)}",
        f"- Workflow ID: {project.workflow_id or '(none)'}",
        f"- 耗时: {_format_duration(elapsed)}",
        f"- 阶段: {len(project.phases)}",
        f"- 代理: {completed_agents}/{total_agents} 完成，{failed_agents} 失败，{cached_agents} 缓存",
        f"- Token: {total_tokens}",
    ]
    return "\n".join(lines)


def _phase_process(project: WorkflowProject) -> str:
    if not project.phases:
        return "本次 Workflow 没有记录阶段。"

    lines: list[str] = []
    for idx, phase in enumerate(project.phases, 1):
        agents = phase.agents
        total = len(agents)
        done = sum(1 for agent in agents if agent.status in (AgentStatus.DONE, AgentStatus.CACHED))
        failed = sum(1 for agent in agents if agent.status == AgentStatus.FAILED)
        cancelled = sum(1 for agent in agents if agent.status == AgentStatus.CANCELLED)
        duration = ""
        if phase.started_at and phase.finished_at:
            duration = f"，耗时 {_format_duration(max(0.0, phase.finished_at - phase.started_at))}"
        lines.append(f"### 阶段 {idx}: {phase.title}")
        lines.append(f"- 汇总: {done}/{total} 完成，{failed} 失败，{cancelled} 已取消{duration}")
        for agent in agents:
            agent_line = (
                f"- [{_status_text(agent.status)}] {agent.label or 'agent'}"
                f" tool={agent.tool or '(none)'}"
            )
            if agent.duration_s:
                agent_line += f" duration={_format_duration(agent.duration_s)}"
            if agent.token_usage:
                agent_line += f" tokens={agent.token_usage}"
            if agent.task_summary:
                agent_line += f"\n  - task: {agent.task_summary}"
            if agent.error:
                agent_line += f"\n  - error: {agent.error}"
            lines.append(agent_line)
        lines.append("")
    return "\n".join(lines).strip()


def _result_sections(project: WorkflowProject) -> list[_ReportSection]:
    payload = _parse_json_payload(project.result)
    sections: list[_ReportSection] = []

    if isinstance(payload, dict):
        priority = [
            "final_report",
            "report",
            "summary",
            "conclusion",
            "result",
            "output",
            "verification",
            "reviews",
            "risks",
            "findings",
            "recommendations",
            "next_steps",
        ]
        seen: set[str] = set()
        for key in priority:
            if key not in payload:
                continue
            body = _format_full_value(payload.get(key))
            if body:
                sections.append(_ReportSection(_humanize_key(key), body, expanded=True))
                seen.add(key)

        for key in ("agent_outputs", "raw_outputs", "worker_outputs", "parallel_results", "results"):
            if key in payload and key not in seen:
                body = _format_full_value(payload.get(key))
                if body:
                    sections.append(_ReportSection(_humanize_key(key), body, expanded=False))
                    seen.add(key)

        remaining = {key: value for key, value in payload.items() if key not in seen}
        if remaining:
            sections.append(_ReportSection("其他结果字段", _pretty_json(remaining), expanded=False))
    elif payload is not None:
        sections.append(_ReportSection("执行结果", _format_full_value(payload), expanded=True))
    elif project.result:
        sections.append(_ReportSection("执行结果", str(project.result), expanded=True))
    else:
        sections.append(_ReportSection("执行结果", "本次 Workflow 没有返回最终结果。", expanded=True))

    return sections


def _report_sections(project: WorkflowProject) -> list[_ReportSection]:
    sections = [
        _ReportSection("运行摘要", _workflow_summary(project), expanded=True),
        *_result_sections(project),
        _ReportSection("执行过程", _phase_process(project), expanded=False),
        _ReportSection("原始结果 JSON", project.result or "", expanded=False),
        _ReportSection("原始 Workflow 状态 JSON", _pretty_json(project.to_dict()), expanded=False),
    ]
    if project.error:
        sections.insert(1, _ReportSection("错误信息", project.error, expanded=True))
    return sections


def build_workflow_report_markdown(project: WorkflowProject) -> str:
    """Return a full, untruncated Markdown report for a Workflow run."""
    title = project.name or "Workflow"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# {title} 完整报告",
        "",
        f"生成时间: {generated_at}",
        "",
    ]
    for section in _report_sections(project):
        lines.append(f"## {section.title}")
        lines.append(section.body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_workflow_report_html(project: WorkflowProject, markdown: str | None = None) -> str:
    """Return a self-contained, static HTML report for a Workflow run."""
    title = project.name or "Workflow"
    markdown = markdown if markdown is not None else build_workflow_report_markdown(project)
    sections = _report_sections(project)

    nav_links = "\n".join(
        f'<a href="#section-{idx}">{html.escape(section.title)}</a>' for idx, section in enumerate(sections)
    )
    section_html = []
    for idx, section in enumerate(sections):
        body = html.escape(section.body)
        search_text = html.escape(f"{section.title}\n{section.body}".lower(), quote=True)
        expanded_attr = " open" if section.expanded else ""
        section_html.append(
            f"""
      <details class="report-section" id="section-{idx}" data-search="{search_text}"{expanded_attr}>
        <summary>
          <span>{html.escape(section.title)}</span>
          <button type="button" onclick="toggleSection('section-{idx}'); event.preventDefault();">折叠/展开</button>
          <button type="button" onclick="copyText('section-body-{idx}'); event.preventDefault();">复制</button>
        </summary>
        <pre id="section-body-{idx}">{body}</pre>
      </details>"""
        )

    escaped_markdown = html.escape(markdown)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} 完整报告</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #1f2329;
      --muted: #646a73;
      --line: #dee0e3;
      --accent: #3370ff;
      --code: #f2f3f5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 2;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(8px);
    }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 18px 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; line-height: 1.25; }}
    .meta {{ color: var(--muted); }}
    .toolbar {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 14px;
      flex-wrap: wrap;
    }}
    input[type="search"] {{
      min-width: min(480px, 100%);
      flex: 1;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
    }}
    button {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      cursor: pointer;
      padding: 7px 10px;
      font: inherit;
    }}
    button.primary {{ border-color: var(--accent); color: var(--accent); }}
    nav {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    nav a {{
      color: var(--accent);
      text-decoration: none;
      border: 1px solid #d6e4ff;
      border-radius: 999px;
      padding: 4px 9px;
      background: #f0f5ff;
    }}
    main .wrap {{ padding-top: 20px; padding-bottom: 40px; }}
    details.report-section {{
      margin: 12px 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
    }}
    details[hidden] {{ display: none; }}
    summary {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      cursor: pointer;
      font-weight: 700;
    }}
    summary span {{ flex: 1; min-width: 160px; }}
    pre {{
      margin: 0;
      padding: 14px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      background: var(--code);
      font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .markdown-source {{ margin-top: 18px; }}
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>{html.escape(title)} 完整报告</h1>
      <div class="meta">生成时间: {generated_at} · 状态: {html.escape(_status_text(project.status))}</div>
      <div class="toolbar">
        <input id="filter" type="search" placeholder="搜索报告内容" oninput="filterSections(this.value)">
        <button class="primary" type="button" onclick="setAllSections(true)">全部展开</button>
        <button type="button" onclick="setAllSections(false)">全部折叠</button>
      </div>
      <nav>{nav_links}</nav>
    </div>
  </header>
  <main>
    <div class="wrap">
      {''.join(section_html)}
      <details class="report-section markdown-source">
        <summary><span>完整 Markdown 源文本</span><button type="button" onclick="copyText('markdown-source'); event.preventDefault();">复制</button></summary>
        <pre id="markdown-source">{escaped_markdown}</pre>
      </details>
    </div>
  </main>
  <script>
    function toggleSection(id) {{
      const section = document.getElementById(id);
      if (section) section.open = !section.open;
    }}
    function setAllSections(open) {{
      document.querySelectorAll('details.report-section').forEach((section) => {{
        if (!section.hidden) section.open = open;
      }});
    }}
    function filterSections(query) {{
      const q = (query || '').trim().toLowerCase();
      document.querySelectorAll('details.report-section').forEach((section) => {{
        const haystack = section.getAttribute('data-search') || section.textContent.toLowerCase();
        section.hidden = q.length > 0 && !haystack.includes(q);
      }});
    }}
    async function copyText(id) {{
      const node = document.getElementById(id);
      if (!node || !navigator.clipboard) return;
      await navigator.clipboard.writeText(node.innerText || node.textContent || '');
    }}
  </script>
</body>
</html>
"""


def write_workflow_report_files(
    project: WorkflowProject,
    root_path: str,
    *,
    cache_root: str | None = None,
) -> WorkflowReportFiles:
    """Write full Workflow report files under the mirrored GhostAP cache root."""
    root = Path(workflow_project_cache_root(root_path, cache_root))
    report_dir = root / _REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_slug = _safe_slug(project.workflow_id or project.name or "workflow")
    suffix = uuid.uuid4().hex[:8] if not project.workflow_id else stamp
    run_id = _safe_slug(f"{base_slug}-{suffix}")
    html_filename = f"{run_id}.html"
    markdown_filename = f"{run_id}.md"
    html_path = report_dir / html_filename
    markdown_path = report_dir / markdown_filename

    markdown = build_workflow_report_markdown(project)
    html_report = build_workflow_report_html(project, markdown)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_report, encoding="utf-8")

    return WorkflowReportFiles(
        run_id=run_id,
        html_path=str(html_path),
        markdown_path=str(markdown_path),
        html_filename=html_filename,
        markdown_filename=markdown_filename,
    )
