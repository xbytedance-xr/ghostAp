/**
 * code-audit — Multi-perspective code audit workflow.
 *
 * Performs parallel code review from security, performance, and maintainability
 * perspectives, then merges findings into a unified report.
 */

export const meta = {
  name: "code-audit",
  description: "Multi-perspective code audit with parallel reviewers and unified report",
  phases: [
    { title: "Analysis", detail: "Analyze codebase structure and identify targets" },
    { title: "Review", detail: "Parallel review from multiple perspectives" },
    { title: "Synthesis", detail: "Merge findings into unified report" }
  ],
  maxConcurrent: 4,
  tools: ["claude", "coco", "aiden"]
};

export default async function main(args = {}) {
  const target = args.target || ".";
  const focus = args.focus || "";

  // Phase 1: Analysis
  phase("Analysis");
  log(`Analyzing codebase at: ${target}`);

  const analysis = await agent({
    prompt: `Analyze the codebase structure at "${target}". ${focus ? `Focus area: ${focus}.` : ""}
Identify the most important files and modules that should be reviewed.
Output a JSON object with:
- "files": array of file paths to review (max 10 most critical)
- "summary": brief description of the codebase architecture
- "risk_areas": array of areas that may have issues`,
    tool: "claude",
    schema: { files: [], summary: "", risk_areas: [] },
    label: "structure-analysis",
    timeout: 180,
  });

  if (analysis && analysis.error) {
    return { error: analysis.error, stage: "Analysis" };
  }

  const files = analysis.files || [];
  const fileList = files.join(", ");
  log(`Identified ${files.length} files for review`);

  // Phase 2: Parallel Review
  phase("Review");
  log("Starting parallel reviews from 3 perspectives");

  const reviews = await parallel([
    {
      prompt: `You are a security auditor. Review these files for security vulnerabilities:
${fileList}

Look for: injection flaws, authentication issues, data exposure, insecure defaults, missing validation.
Output JSON: { "findings": [{ "file": "", "line": 0, "severity": "high|medium|low", "issue": "", "fix": "" }] }`,
      tool: "claude",
      role: "Security Auditor",
      schema: { findings: [] },
      label: "security-review",
      timeout: 180,
    },
    {
      prompt: `You are a performance engineer. Review these files for performance issues:
${fileList}

Look for: N+1 queries, unnecessary allocations, blocking I/O, missing caching, inefficient algorithms.
Output JSON: { "findings": [{ "file": "", "line": 0, "severity": "high|medium|low", "issue": "", "fix": "" }] }`,
      tool: "aiden",
      role: "Performance Engineer",
      schema: { findings: [] },
      label: "performance-review",
      timeout: 180,
    },
    {
      prompt: `You are a senior maintainability reviewer. Review these files for code quality:
${fileList}

Look for: code duplication, unclear naming, missing error handling, tight coupling, missing tests.
Output JSON: { "findings": [{ "file": "", "line": 0, "severity": "high|medium|low", "issue": "", "fix": "" }] }`,
      tool: "coco",
      role: "Maintainability Reviewer",
      schema: { findings: [] },
      label: "maintainability-review",
      timeout: 180,
    },
  ]);

  // Phase 3: Synthesis
  phase("Synthesis");

  const allFindings = [];
  const perspectives = ["Security", "Performance", "Maintainability"];
  reviews.forEach((r, i) => {
    if (r?.error) {
      allFindings.push({
        perspective: perspectives[i],
        severity: "medium",
        issue: `Reviewer failed: ${r.error}`,
        fix: "Retry the review or inspect workflow logs",
      });
      return;
    }
    const findings = r?.findings || [];
    findings.forEach(f => {
      allFindings.push({ ...f, perspective: perspectives[i] });
    });
  });

  log(`Total findings: ${allFindings.length} — synthesizing report`);

  const report = await agent({
    prompt: `Synthesize these code audit findings into a unified report.

Findings from 3 perspectives:
${JSON.stringify(allFindings, null, 2)}

Architecture summary: ${analysis.summary}

Create a prioritized report with:
1. Executive summary (2-3 sentences)
2. Critical issues (must fix)
3. Important issues (should fix)
4. Minor issues (nice to fix)
5. Positive observations

Format as markdown.`,
    tool: "aiden",
    label: "report-synthesis",
    timeout: 180,
  });

  if (report && report.error) {
    return { error: report.error, stage: "Synthesis", findings: allFindings };
  }

  return report;
}
