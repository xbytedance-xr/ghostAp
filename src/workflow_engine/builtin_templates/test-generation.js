/**
 * test-generation — Test coverage gap analysis and case generation workflow.
 *
 * Identifies modules lacking test coverage, produces test cases in parallel,
 * and emits a structured JSON summary.
 */

export const meta = {
  name: "test-generation",
  description: "Find uncovered modules, generate test cases in parallel, and produce a summary",
  phases: [
    { title: "Coverage Analysis", detail: "Identify modules lacking test coverage" },
    { title: "Case Generation", detail: "Parallel agents generate test cases for each uncovered module" },
    { title: "Summary", detail: "Structured JSON summary of generated test artifacts" }
  ],
  maxConcurrent: 8,
  tools: ["acp", "claude", "coco"]
};

export default async function main(args = {}) {
  const target = args.target || ".";
  const moduleFilter = args.modules || "";

  // Phase 1: Coverage Analysis
  phase("Coverage Analysis");
  log(`Analyzing test coverage for: ${target}`);

  const coverage = await agent({
    prompt: `You are a test engineer. Analyze the project at "${target}" ${moduleFilter ? `(modules: ${moduleFilter})` : ""}.
Identify modules or files that have minimal or no test coverage.

Please prefer using subagent workflows where possible.

Output JSON: { "uncovered_modules": [{ "module": "", "file": "", "coverage_estimate": 0-100, "priority": "high|medium|low", "notes": "" }] }`,
    tool: "acp",
    role: "测试工程师",
    schema: { uncovered_modules: [] },
    label: "coverage-analysis",
    timeout: 180,
  });

  if (coverage && coverage.error) {
    return { error: coverage.error, stage: "Coverage Analysis" };
  }

  const modules = coverage.uncovered_modules || [];
  log(`Identified ${modules.length} uncovered modules`);

  // Phase 2: Case Generation
  phase("Case Generation");
  log("Generating test cases in parallel");

  const genTasks = modules.slice(0, 16).map((m, i) => ({
    prompt: `You are a test engineer. Generate test cases for:

Module: ${m.module}
File: ${m.file}
Current coverage estimate: ${m.coverage_estimate}%
Priority: ${m.priority}
Notes: ${m.notes || ""}

Please prefer using subagent workflows where possible.

Produce self-contained test code covering:
- happy paths
- edge cases
- error conditions

Output JSON: { "module": "", "file": "", "test_code": "", "test_count": 0, "types_tested": [] }`,
    tool: "claude",
    role: "测试工程师",
    schema: { module: "", file: "", test_code: "", test_count: 0, types_tested: [] },
    label: `gen-tests-${i}`,
    phase: "Case Generation",
    timeout: 180,
  }));

  const results = genTasks.length > 0 ? await parallel(genTasks) : [];
  log(`Generated test cases for ${results.length} modules`);

  // Phase 3: Summary
  phase("Summary");

  const totalTests = results.reduce((s, r) => s + (r?.test_count || 0), 0);

  const summary = await agent({
    prompt: `Summarize this test generation run.

Target: ${target}
Modules analyzed: ${modules.length}
Test cases generated: ${totalTests}

Please prefer using subagent workflows where possible.

Output JSON:
{
  "modules_analyzed": ${modules.length},
  "total_test_cases": ${totalTests},
  "generated_files": [],
  "coverage_improvement_estimate": 0,
  "recommendations": []
}`,
    tool: "acp",
    schema: {
      modules_analyzed: 0,
      total_test_cases: 0,
      generated_files: [],
      coverage_improvement_estimate: 0,
      recommendations: [],
    },
    label: "test-summary",
    timeout: 180,
  });

  if (summary && summary.error) {
    return { error: summary.error, stage: "Summary", partial: { coverage, results } };
  }

  return {
    summary,
    findings: modules,
    phaseResults: { coverage, results, summary },
  };
}
