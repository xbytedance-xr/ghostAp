/**
 * performance-analysis — Performance bottleneck scanning and recommendation workflow.
 *
 * Scans the project for performance hotspots, performs deep parallel analysis,
 * and produces a structured JSON summary with actionable recommendations.
 */

export const meta = {
  name: "performance-analysis",
  description: "Scan performance hotspots, perform deep analysis, and produce a structured recommendation summary",
  phases: [
    { title: "Bottleneck Scan", detail: "Identify performance hotspots across the codebase" },
    { title: "Deep Analysis", detail: "Parallel agents analyze each hotspot in depth" },
    { title: "Recommendations", detail: "Structured JSON summary with actionable improvements" }
  ],
  maxConcurrent: 8,
  tools: ["acp", "claude", "coco"]
};

export default async function main(args = {}) {
  const target = args.target || ".";
  const focus = args.focus || "";

  // Phase 1: Bottleneck Scan
  phase("Bottleneck Scan");
  log(`Scanning ${target} for performance bottlenecks`);

  const scan = await agent({
    prompt: `You are a performance engineer. Scan the project at "${target}" ${focus ? `(focus: ${focus})` : ""}.
Look for performance hotspots: N+1 queries, blocking I/O, unnecessary allocations, inefficient algorithms,
missing caching, hot loops, and slow serialization paths.

Please prefer using subagent workflows where possible.

Output JSON: { "hotspots": [{ "file": "", "description": "", "category": "io|cpu|memory|algorithm|cache|network", "severity": "high|medium|low", "estimated_impact": "" }] }`,
    tool: "acp",
    role: "性能工程师",
    schema: { hotspots: [] },
    label: "bottleneck-scan",
  });

  const hotspots = scan.hotspots || [];
  log(`Found ${hotspots.length} performance hotspots`);

  // Phase 2: Deep Analysis
  phase("Deep Analysis");
  log("Running deep analysis on each hotspot in parallel");

  const analysisTasks = hotspots.slice(0, 12).map((h, i) => ({
    prompt: `You are a performance engineer. Perform deep analysis for this hotspot:

File: ${h.file}
Category: ${h.category}
Severity: ${h.severity}
Description: ${h.description}
Estimated impact: ${h.estimated_impact || ""}

Please prefer using subagent workflows where possible.

Produce a focused analysis with root cause, cost estimate, and a concrete optimization sketch.

Output JSON:
{
  "file": "",
  "root_cause": "",
  "cost_estimate": "",
  "optimization": "",
  "before_code": "",
  "after_code": "",
  "expected_gain": ""
}`,
    tool: "claude",
    role: "性能工程师",
    schema: {
      file: "",
      root_cause: "",
      cost_estimate: "",
      optimization: "",
      before_code: "",
      after_code: "",
      expected_gain: "",
    },
    label: `deep-${i}`,
    phase: "Deep Analysis",
  }));

  const analyses = analysisTasks.length > 0 ? await parallel(analysisTasks) : [];
  log(`Completed ${analyses.length} deep analyses`);

  // Phase 3: Recommendations
  phase("Recommendations");

  const summary = await agent({
    prompt: `Summarize this performance analysis run for "${target}".

Hotspots scanned: ${hotspots.length}
Deep analyses produced: ${analyses.length}

Please prefer using subagent workflows where possible.

Output a JSON summary with prioritized recommendations.

Output JSON:
{
  "hotspot_count": ${hotspots.length},
  "severity_breakdown": { "high": 0, "medium": 0, "low": 0 },
  "top_recommendations": [],
  "estimated_overall_gain": "",
  "next_steps": []
}`,
    tool: "acp",
    schema: {
      hotspot_count: 0,
      severity_breakdown: { high: 0, medium: 0, low: 0 },
      top_recommendations: [],
      estimated_overall_gain: "",
      next_steps: [],
    },
    label: "perf-summary",
  });

  return {
    summary,
    findings: hotspots,
    phaseResults: { scan, analyses, summary },
  };
}
