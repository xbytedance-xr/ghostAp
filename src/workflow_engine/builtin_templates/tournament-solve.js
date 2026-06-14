/**
 * tournament-solve — Tournament-based problem solving.
 *
 * Spawns multiple agents to independently solve a task using different
 * strategies, then runs pairwise elimination to find the best solution.
 * Winner undergoes adversarial verification before delivery.
 */

export const meta = {
  name: "tournament-solve",
  description: "Competitive multi-agent problem solving with tournament elimination",
  phases: [
    { title: "Analysis", detail: "Analyze problem and determine competition strategy" },
    { title: "Competition", detail: "Multiple agents compete on the same task" },
    { title: "Elimination", detail: "Pairwise judging to determine the best" },
    { title: "Verification", detail: "Adversarial verification of the winner" },
  ],
  maxConcurrent: 8,
  tools: ["coco", "claude", "aiden", "gemini"],
  patterns: ["tournament", "verify"],
};

export default async function main(args = {}) {
  const task = args.task || workflowArgs.task || "Solve the given problem";
  const contestantCount = args.contestants || 4;

  // Phase 1: Analysis
  phase("Analysis");
  log("Analyzing problem to determine competition angles...");

  const analysis = await agent(`Analyze this task and suggest ${contestantCount} distinctly different strategies to solve it.
Each strategy should have a different philosophy (e.g., brute-force vs elegant, minimal vs comprehensive, safe vs innovative).

Task: ${task}

Respond with JSON: { "strategies": [{ "name": "", "philosophy": "", "best_tool": "" }], "evaluation_criteria": "" }`, {
    tool: "claude",
    role: "strategist",
    label: "strategy-analysis",
    schema: { strategies: [], evaluation_criteria: "" },
  });

  const strategies = (analysis.strategies || []).slice(0, contestantCount);
  const criteria = analysis.evaluation_criteria || "correctness, completeness, efficiency";

  // Phase 2-3: Tournament
  phase("Competition");
  log(`Running tournament with ${strategies.length} contestants...`);

  const tools = ["coco", "claude", "aiden", "gemini", "codex", "traex"];

  const { winner, winnerLabel, bracket, rounds } = await tournament(
    strategies.map((strategy, i) => ({
      prompt: `Solve this task using the "${strategy.name}" strategy.
Philosophy: ${strategy.philosophy}

Task: ${task}

Provide a complete, production-ready solution following your assigned strategy.
Be thorough and demonstrate why your approach is superior.`,
      tool: strategy.best_tool || tools[i % tools.length],
      role: strategy.name,
      label: `contestant-${strategy.name}`,
    })),
    null,
    { judgeTool: "claude", task: task, criteria: criteria }
  );

  log(`Tournament complete: ${rounds} rounds, winner: ${winnerLabel}`);
  log(`Bracket: ${bracket.map(m => `${m.winner} > ${m.loser}`).join(" → ")}`);

  // Phase 4: Verify the winner
  phase("Verification");
  log("Adversarial verification of tournament winner...");

  const { accepted, output: verified, feedback } = await verify(winner, {
    criteria: criteria,
    verifiers: [
      { tool: "claude", role: "logic_adversary", focus: "Find logical errors and incorrect assumptions" },
      { tool: "aiden", role: "quality_adversary", focus: "Find code quality issues and maintainability problems" },
    ],
    maxRounds: 2,
    reviseTool: "coco",
  });

  if (!accepted) {
    log(`Verification raised concerns: ${feedback}`);
  } else {
    log("Winner passed adversarial verification");
  }

  return {
    solution: verified,
    approach: winnerLabel,
    tournament_bracket: bracket,
    verified: accepted,
    remaining_concerns: accepted ? null : feedback,
  };
}
