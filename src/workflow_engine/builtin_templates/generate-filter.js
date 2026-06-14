/**
 * generate-filter — Generate-and-Filter creative exploration.
 *
 * Generates multiple diverse solutions/ideas, then filters and ranks them
 * using AI-based evaluation. Combines with tournament for final selection.
 */

export const meta = {
  name: "generate-filter",
  description: "Generate multiple candidates and filter to the best via AI evaluation",
  phases: [
    { title: "Exploration", detail: "Generate diverse candidate solutions" },
    { title: "Evaluation", detail: "Filter and rank candidates" },
    { title: "Refinement", detail: "Polish the top candidates" },
    { title: "Selection", detail: "Final tournament selection" },
  ],
  maxConcurrent: 8,
  tools: ["coco", "claude", "aiden", "gemini"],
  patterns: ["generate", "tournament", "verify"],
};

export default async function main(args = {}) {
  const task = args.task || workflowArgs.task || "Generate solutions";
  const candidateCount = args.candidates || 6;
  const topK = args.topK || 3;
  const criteria = args.criteria || "quality, originality, feasibility, correctness";

  // Phase 1: Generate diverse candidates
  phase("Exploration");
  log(`Generating ${candidateCount} diverse candidate solutions...`);

  const tools = ["coco", "claude", "aiden", "gemini", "codex", "traex"];
  const perspectives = [
    "minimalist — fewest lines, simplest approach",
    "comprehensive — cover every edge case, maximum robustness",
    "innovative — novel approach, unconventional thinking",
    "pragmatic — fastest to ship, proven patterns",
    "elegant — beautiful abstractions, clean architecture",
    "performant — optimized for speed and resource efficiency",
  ];

  const topCandidates = await generate(
    candidateCount,
    (i) => ({
      prompt: `Generate a solution for this task with a ${perspectives[i % perspectives.length]} approach.

Task: ${task}

Your philosophy: ${perspectives[i % perspectives.length]}

Provide a complete, self-contained solution. Explain your design reasoning briefly.`,
      tool: tools[i % tools.length],
      role: `generator-${perspectives[i % perspectives.length].split(" ")[0]}`,
    }),
    null,
    { topK, criteria, filterTool: "claude" }
  );

  log(`Filtered to top ${topCandidates.length} candidates`);

  // Phase 2: Refine top candidates
  phase("Refinement");
  log("Refining top candidates...");

  const refined = await parallel(
    topCandidates.map((candidate, i) => ({
      prompt: `Refine and improve this solution while preserving its core approach.
Fix any issues, fill gaps, and polish it to production quality.

Solution to refine:
${typeof candidate === 'string' ? candidate : JSON.stringify(candidate)}

Original task: ${task}

Provide the refined, complete solution.`,
      tool: tools[(i + 1) % tools.length],
      role: "refiner",
      label: `refine-${i}`,
    }))
  );

  // Phase 3: Tournament to find the best
  phase("Selection");
  log("Running final tournament...");

  if (refined.length < 2) {
    const { output } = await verify(refined[0], {
      criteria,
      verifiers: [{ tool: "claude", role: "final_check", focus: "Last quality check" }],
      maxRounds: 1,
    });
    return output;
  }

  const { winner, winnerLabel, bracket } = await tournament(
    refined.map((sol, i) => ({
      prompt: `Present this as your final solution:\n${typeof sol === 'string' ? sol : JSON.stringify(sol)}`,
      tool: tools[i % tools.length],
      label: `finalist-${i}`,
    })),
    null,
    { judgeTool: "claude", task, criteria }
  );

  log(`Tournament winner: ${winnerLabel}`);

  // Final verification
  const { output } = await verify(winner, {
    criteria,
    verifiers: [
      { tool: "aiden", role: "final_verifier", focus: "Correctness and completeness" },
    ],
    maxRounds: 1,
  });

  return output;
}
