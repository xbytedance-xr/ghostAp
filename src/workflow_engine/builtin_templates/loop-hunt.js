/**
 * loop-hunt — Loop-Until-Done iterative discovery.
 *
 * Iteratively searches for issues, bugs, or improvements until convergence.
 * Each iteration builds on previous findings, with convergence detection
 * to stop when no new discoveries are being made.
 */

export const meta = {
  name: "loop-hunt",
  description: "Iterative discovery with convergence detection — bug hunting, auditing, scanning",
  phases: [
    { title: "Initial Scan", detail: "First-pass broad discovery" },
    { title: "Deep Dive", detail: "Iterative deep exploration until convergence" },
    { title: "Synthesis", detail: "Consolidate all findings into actionable report" },
  ],
  maxConcurrent: 4,
  tools: ["coco", "claude", "aiden"],
  patterns: ["loop", "fanout", "verify"],
};

export default async function main(args = {}) {
  const target = args.target || workflowArgs.target || "the codebase";
  const huntType = args.type || workflowArgs.type || "bugs and issues";
  const maxIterations = args.maxIterations || 6;

  // Phase 1: Initial broad scan from multiple perspectives
  phase("Initial Scan");
  log(`Starting broad scan for ${huntType} in ${target}...`);

  const initialFindings = await fanout(`Scan ${target} for ${huntType}`, [
    {
      prompt: `Do a broad scan for ${huntType} in ${target}. Focus on HIGH severity issues first.
Report findings as JSON: { "findings": [{ "severity": "high|medium|low", "category": "", "description": "", "location": "", "evidence": "" }] }`,
      tool: "claude",
      role: "broad_scanner",
      label: "broad-scan",
      schema: { findings: [] },
    },
    {
      prompt: `Look for subtle, hard-to-find ${huntType} in ${target} that a broad scan might miss.
Focus on edge cases, race conditions, and non-obvious issues.
Report findings as JSON: { "findings": [{ "severity": "high|medium|low", "category": "", "description": "", "location": "", "evidence": "" }] }`,
      tool: "aiden",
      role: "deep_scanner",
      label: "deep-scan",
      schema: { findings: [] },
    },
  ], { synthesize: false });

  const allFindings = [];
  for (const scan of initialFindings) {
    if (scan && scan.findings) allFindings.push(...scan.findings);
  }
  log(`Initial scan found ${allFindings.length} issues`);

  // Phase 2: Iterative deep dive
  phase("Deep Dive");
  log("Starting iterative deep dive with convergence detection...");

  const { results, iterations, stoppedBy } = await loop(
    async (i, prev) => {
      const knownFindings = allFindings
        .map(f => `- [${f.severity}] ${f.description} @ ${f.location}`)
        .join('\n');

      const newFindings = await agent(`Iteration ${i + 1}: Continue hunting for ${huntType} in ${target}.

ALREADY KNOWN findings (do NOT re-report these):
${knownFindings || "(none yet)"}

Your job: Find NEW issues that are NOT in the above list.
Look deeper — check less obvious paths, unusual configurations, corner cases.
If you genuinely cannot find any new issues, set "no_new_findings" to true.

Respond with JSON: { "findings": [{ "severity": "high|medium|low", "category": "", "description": "", "location": "", "evidence": "" }], "no_new_findings": false, "search_strategy": "" }`, {
        tool: ["claude", "coco", "aiden"][i % 3],
        role: `hunter-round-${i}`,
        label: `hunt-${i}`,
        schema: { findings: [], no_new_findings: false, search_strategy: "" },
      });

      if (newFindings && newFindings.findings) {
        for (const f of newFindings.findings) {
          const isDuplicate = allFindings.some(existing =>
            existing.description === f.description || existing.location === f.location
          );
          if (!isDuplicate) {
            allFindings.push(f);
          }
        }
      }

      return newFindings;
    },
    {
      maxIterations,
      stopWhen: (result) => result?.no_new_findings === true,
      convergenceCheck: (curr, prev) => {
        const currNew = (curr?.findings || []).length;
        const prevNew = (prev?.findings || []).length;
        return currNew === 0 && prevNew === 0;
      },
    }
  );

  log(`Deep dive complete: ${iterations} iterations, stopped by ${stoppedBy}`);
  log(`Total findings: ${allFindings.length}`);

  // Phase 3: Synthesize and verify findings
  phase("Synthesis");
  log("Synthesizing and verifying findings...");

  const findingsReport = allFindings
    .map((f, i) => `${i + 1}. [${f.severity}] ${f.category}: ${f.description}\n   Location: ${f.location}\n   Evidence: ${f.evidence}`)
    .join('\n\n');

  const { accepted, output: verifiedReport } = await verify(findingsReport, {
    criteria: "accuracy, no false positives, actionability",
    verifiers: [
      { tool: "claude", role: "accuracy_checker", focus: "Verify each finding is real (not a false positive)" },
    ],
    maxRounds: 1,
  });

  const finalReport = await agent(`Create a final actionable report from these verified findings.
Group by severity, add recommended fix priority, and provide an executive summary.

Findings:
${typeof verifiedReport === 'string' ? verifiedReport : findingsReport}

Format as a structured markdown report with:
1. Executive Summary (2-3 sentences)
2. Critical Issues (immediate action needed)
3. Major Issues (should fix soon)
4. Minor Issues (nice to have)
5. Recommendations`, {
    tool: "coco",
    role: "report_writer",
    label: "final-report",
  });

  return finalReport;
}
