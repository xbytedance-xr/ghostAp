/**
 * smart-router — Classify-and-Act routing workflow.
 *
 * Classifies incoming tasks and routes to specialized handling pipelines.
 * Demonstrates the classify() + fanout() + verify() pattern composition.
 */

export const meta = {
  name: "smart-router",
  description: "Intelligent task classification with specialized routing and verification",
  phases: [
    { title: "Classification", detail: "Analyze task type, complexity, and dimensions" },
    { title: "Routing", detail: "Route to specialized handler based on classification" },
    { title: "Verification", detail: "Verify output quality before delivery" },
  ],
  maxConcurrent: 6,
  tools: ["coco", "claude", "aiden"],
  patterns: ["classify", "fanout", "verify"],
};

export default async function main(args = {}) {
  const task = args.task || workflowArgs.task || "Complete the given task";

  // Phase 1: Classification
  phase("Classification");
  log("Analyzing task type and routing strategy...");

  const result = await classify(task, {
    "implementation": {
      description: "New feature implementation, code generation, building components",
      handler: async (input) => {
        phase("Routing");
        log("Routed to implementation pipeline");

        const implemented = await fanout(input, [
          { prompt: `Design the architecture for: ${input}`, tool: "claude", role: "architect", label: "design", timeout: 180 },
          { prompt: `Implement the core logic for: ${input}`, tool: "coco", role: "implementer", label: "implement", timeout: 240 },
          { prompt: `Write comprehensive tests for: ${input}`, tool: "aiden", role: "tester", label: "test", timeout: 180 },
        ], { synthesizerTool: "coco", synthesizerRole: "tech_lead", timeout: 180 });

        phase("Verification");
        log("Verifying implementation...");

        const { output } = await verify(implemented, {
          criteria: "correctness, completeness, test coverage",
          verifiers: [
            { tool: "claude", role: "code_reviewer", focus: "Logic errors, edge cases, missing features" },
            { tool: "aiden", role: "quality_gate", focus: "Code quality, maintainability, best practices" },
          ],
          maxRounds: 2,
          reviseTool: "coco",
        });

        return output;
      },
    },
    "debugging": {
      description: "Bug fixing, error diagnosis, troubleshooting",
      handler: async (input) => {
        phase("Routing");
        log("Routed to debugging pipeline");

        const { results, stoppedBy } = await loop(
          async (i, prev) => {
            const context = prev ? `Previous findings: ${typeof prev === 'string' ? prev : JSON.stringify(prev)}` : "No prior findings.";
            const finding = await agent(`Iteration ${i+1} of debugging:
${input}

${context}

Find root cause(s) not yet identified. If all issues are found, set done=true.`, {
              tool: i % 2 === 0 ? "claude" : "coco",
              role: "debugger",
              label: `debug-${i}`,
              schema: { issues: [], root_cause: "", fix_suggestion: "", done: false },
              timeout: 180,
            });
            if (finding && finding.error) {
              return { issues: [], root_cause: "", fix_suggestion: finding.error, done: true, error: finding.error };
            }
            return finding;
          },
          {
            maxIterations: 5,
            stopWhen: (result) => result?.done === true || (result?.root_cause && result.root_cause.length > 20),
          }
        );

        const lastResult = results[results.length - 1];
        log(`Debugging complete after ${results.length} iterations (${stoppedBy})`);
        return lastResult;
      },
    },
    "review": {
      description: "Code review, architecture review, security audit",
      handler: async (input) => {
        phase("Routing");
        log("Routed to multi-perspective review");

        return fanout(input, [
          { prompt: `Security audit: ${input}`, tool: "claude", role: "security_auditor", label: "security", timeout: 180 },
          { prompt: `Architecture review: ${input}`, tool: "aiden", role: "architect", label: "architecture", timeout: 180 },
          { prompt: `Performance review: ${input}`, tool: "coco", role: "perf_expert", label: "performance", timeout: 180 },
          { prompt: `Correctness review: ${input}`, tool: "claude", role: "correctness_checker", label: "correctness", timeout: 180 },
        ], { synthesizerTool: "claude", synthesizerRole: "lead_reviewer", timeout: 180 });
      },
    },
    "optimization": {
      description: "Performance optimization, refactoring, code improvement",
      handler: async (input) => {
        phase("Routing");
        log("Routed to optimization tournament");

        const { winner } = await tournament(
          [
            { prompt: `Optimize for runtime performance: ${input}`, tool: "coco", label: "perf-opt", timeout: 180 },
            { prompt: `Optimize for readability and maintainability: ${input}`, tool: "claude", label: "clean-opt", timeout: 180 },
            { prompt: `Optimize for minimal changes and safety: ${input}`, tool: "aiden", label: "safe-opt", timeout: 180 },
          ],
          null,
          { judgeTool: "claude", task: input, criteria: "correctness, improvement magnitude, safety", timeout: 180 }
        );

        phase("Verification");
        const { output } = await verify(winner, {
          criteria: "no regressions, correctness preserved",
          verifiers: [{ tool: "aiden", role: "regression_checker", focus: "Ensure no regressions", timeout: 180 }],
          maxRounds: 1,
          timeout: 180,
        });

        return output;
      },
    },
  }, { classifierTool: "claude", timeout: 120 });

  return result;
}
