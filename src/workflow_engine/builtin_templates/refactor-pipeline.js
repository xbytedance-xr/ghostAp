/**
 * refactor-pipeline — Structured code refactoring workflow.
 *
 * Discovers refactoring hotspots through parallel static and dependency analysis,
 * produces independent patches for each hotspot, and emits a structured JSON
 * summary with risk level.
 */

export const meta = {
  name: "refactor-pipeline",
  description: "Discover refactoring hotspots, generate patches, and produce a risk-aware summary",
  phases: [
    { title: "Discovery", detail: "Parallel static and dependency analysis to find refactoring hotspots" },
    { title: "Refactoring", detail: "Independent agents produce patches for each identified hotspot" },
    { title: "Validation", detail: "Structured JSON summary of modified files and risk level" }
  ],
  maxConcurrent: 8,
  tools: ["acp", "claude", "coco"]
};

export default async function main(args = {}) {
  const target = args.target || ".";
  const scope = args.scope || "";

  // Phase 1: Discovery
  phase("Discovery");
  log(`Scanning ${target} for refactoring hotspots`);

  const discovery = await parallel([
    {
      prompt: `You are a static analysis specialist. Analyze the codebase at "${target}" ${scope ? `(scope: ${scope})` : ""}.
Identify refactoring hotspots: long functions, deep nesting, cyclomatic complexity hotspots, duplicated blocks,
god classes, unclear naming, and missing error handling.

Please prefer using subagent workflows where possible.

Output JSON: { "hotspots": [{ "file": "", "description": "", "type": "complexity|duplication|naming|error-handling|coupling", "priority": "high|medium|low" }] }`,
      tool: "acp",
      role: "静态分析",
      schema: { hotspots: [] },
      label: "static-analysis",
      phase: "Discovery",
      timeout: 180,
    },
    {
      prompt: `You are a dependency auditor. Audit the codebase at "${target}" ${scope ? `(scope: ${scope})` : ""}.
Find tight coupling between modules, circular dependencies, unused imports, and abstraction boundaries being violated.

Please prefer using subagent workflows where possible.

Output JSON: { "hotspots": [{ "file": "", "description": "", "type": "coupling|circular|unused|boundary", "priority": "high|medium|low" }] }`,
      tool: "claude",
      role: "依赖审计",
      schema: { hotspots: [] },
      label: "dependency-audit",
      phase: "Discovery",
      timeout: 180,
    },
  ]);

  const allHotspots = [];
  discovery.forEach(r => {
    (r?.hotspots || []).forEach(h => allHotspots.push(h));
  });
  log(`Discovered ${allHotspots.length} refactoring hotspots`);

  // Phase 2: Refactoring
  phase("Refactoring");
  log("Producing independent patches for each hotspot");

  const refactorTasks = allHotspots.map((spot, i) => ({
    prompt: `You are a refactoring specialist. Produce a patch for this hotspot:

File: ${spot.file}
Type: ${spot.type}
Priority: ${spot.priority}
Description: ${spot.description}

Please prefer using subagent workflows where possible.

Output a self-contained patch plus an explanation. Include the exact diff or replacement code blocks.

Output JSON: { "file": "", "patch": "", "explanation": "", "tests_impacted": [] }`,
    tool: "claude",
    role: "重构者",
    schema: { file: "", patch: "", explanation: "", tests_impacted: [] },
    label: `refactor-${i}`,
    phase: "Refactoring",
    timeout: 180,
  }));

  const patches = refactorTasks.length > 0 ? await parallel(refactorTasks) : [];
  log(`Produced ${patches.length} patches`);

  // Phase 3: Validation
  phase("Validation");

  const modifiedFiles = patches
    .map(p => p?.file)
    .filter(Boolean);

  const summary = await agent({
    prompt: `Review these refactoring patches and produce a validation summary.

Hotspots found: ${allHotspots.length}
Patches produced: ${patches.length}
Modified files: ${modifiedFiles.join(", ") || "(none)"}

Please prefer using subagent workflows where possible.

Output JSON:
{
  "modified_files": [],
  "risk_level": "high|medium|low",
  "risk_reasoning": "",
  "recommendations": [],
  "verification_steps": []
}`,
    tool: "acp",
    schema: {
      modified_files: [],
      risk_level: "low",
      risk_reasoning: "",
      recommendations: [],
      verification_steps: [],
    },
    label: "validation-summary",
    timeout: 180,
  });

  if (summary && summary.error) {
    return { error: summary.error, stage: "Validation", partial: { discovery, patches } };
  }

  return {
    summary,
    findings: allHotspots,
    phaseResults: { discovery, patches, summary },
  };
}
