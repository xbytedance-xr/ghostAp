/**
 * adversarial-review — Adversarial verification workflow.
 *
 * Implements the fan-out → verify → merge pattern:
 * 1. An implementer produces a solution
 * 2. Multiple adversarial reviewers challenge the solution
 * 3. A judge synthesizes feedback and determines if iteration is needed
 * 4. If needed, the implementer revises (up to max iterations)
 */

export const meta = {
  name: "adversarial-review",
  description: "Adversarial multi-role verification with iterative refinement",
  phases: [
    { title: "Implementation", detail: "Produce initial solution" },
    { title: "Adversarial Review", detail: "Challenge from multiple perspectives" },
    { title: "Judgment", detail: "Synthesize feedback and decide" },
    { title: "Refinement", detail: "Iterate based on feedback (if needed)" }
  ],
  maxConcurrent: 4,
  tools: ["coco", "claude", "aiden"]
};

export default async function main(args = {}) {
  const task = args.task || "Implement the requested feature";
  const maxIterations = args.maxIterations || 2;
  const reviewerCount = args.reviewerCount || 3;

  let currentSolution = null;
  let iteration = 0;
  let approved = false;

  while (iteration < maxIterations && !approved) {
    iteration++;
    log(`--- Iteration ${iteration}/${maxIterations} ---`);

    // Phase 1: Implementation (or Refinement)
    if (iteration === 1) {
      phase("Implementation");
      log("Generating initial implementation");

      currentSolution = await agent({
        prompt: `You are a senior engineer. Complete this task:

${task}

Provide a complete, production-ready implementation. Include:
- All necessary code changes
- Error handling
- Brief inline comments for complex logic

Output your complete solution.`,
        tool: "coco",
        role: "Senior Engineer",
        label: `implement-v${iteration}`,
        timeout: 240,
      });
    } else {
      phase("Refinement");
      log(`Refining solution based on feedback (iteration ${iteration})`);

      currentSolution = await agent({
        prompt: `You are a senior engineer. Revise your previous solution based on reviewer feedback.

Original task: ${task}

Your previous solution:
${currentSolution}

Reviewer feedback from last round:
${JSON.stringify(lastFeedback, null, 2)}

Address ALL valid concerns. Ignore feedback that is incorrect or would degrade the solution.
Output your revised complete solution.`,
        tool: "coco",
        role: "Senior Engineer",
        label: `refine-v${iteration}`,
        timeout: 240,
      });
    }

    if (currentSolution && currentSolution.error) {
      return { error: currentSolution.error, stage: "Implementation", iteration };
    }

    // Phase 2: Adversarial Review
    phase("Adversarial Review");
    log(`Launching ${reviewerCount} adversarial reviewers`);

    const reviewerRoles = [
      { role: "Security Adversary", tool: "claude", focus: "Find security vulnerabilities, injection points, auth bypasses, data leaks" },
      { role: "Correctness Adversary", tool: "aiden", focus: "Find logic errors, edge cases, race conditions, incorrect assumptions" },
      { role: "Architecture Adversary", tool: "claude", focus: "Find coupling issues, scalability problems, maintenance burden, design flaws" },
    ].slice(0, reviewerCount);

    const reviews = await parallel(
      reviewerRoles.map((reviewer, idx) => ({
        prompt: `You are a ${reviewer.role}. Your job is to FIND PROBLEMS in this solution. Be adversarial — try to break it.

Task that was implemented: ${task}

Solution to review:
${currentSolution}

Focus: ${reviewer.focus}

Rules:
- Only report REAL issues (not stylistic preferences)
- Each issue must include a concrete example or scenario that triggers it
- Rate severity: critical (must fix) / major (should fix) / minor (nice to fix)

Output JSON:
{
  "issues": [{ "severity": "critical|major|minor", "description": "", "example": "", "suggestion": "" }],
  "overall_quality": "poor|acceptable|good|excellent",
  "approve": true/false
}`,
        tool: reviewer.tool,
        role: reviewer.role,
        schema: { issues: [], overall_quality: "", approve: false },
        label: `review-${reviewer.role.toLowerCase().replace(/\s+/g, "-")}-v${iteration}`,
        timeout: 180,
      }))
    );

    // Phase 3: Judgment
    phase("Judgment");

    const allIssues = [];
    let approvalCount = 0;
    reviews.forEach(r => {
      if (r?.error) {
        allIssues.push({ severity: "major", description: `Reviewer failed: ${r.error}`, example: "", suggestion: "Retry or inspect reviewer logs" });
        return;
      }
      if (r?.approve) approvalCount++;
      (r?.issues || []).forEach(issue => allIssues.push(issue));
    });

    log(`Reviews: ${approvalCount}/${reviewerCount} approved, ${allIssues.length} issues found`);

    // Unanimous approval or final iteration → done
    if (approvalCount === reviewerCount) {
      approved = true;
      log("All reviewers approved — solution accepted");
    } else if (iteration >= maxIterations) {
      log("Max iterations reached — proceeding with judge synthesis");
    } else {
      // Filter to actionable feedback for next iteration
      var lastFeedback = allIssues.filter(i => i.severity !== "minor");
      log(`${lastFeedback.length} actionable issues — proceeding to refinement`);
    }
  }

  // Final synthesis
  const verdict = await agent({
    prompt: `You are a technical lead making the final decision.

Task: ${task}
Iterations completed: ${iteration}
Final solution:
${currentSolution}

${approved ? "All reviewers approved this solution." : "Max iterations reached. Some concerns remain."}

Provide:
1. Final verdict: APPROVE or APPROVE_WITH_NOTES
2. Summary of the solution's strengths
3. Any remaining risks or caveats
4. Confidence level (high/medium/low)

Format as markdown.`,
    tool: "aiden",
    role: "Technical Lead",
    label: "final-verdict",
    timeout: 180,
  });

  if (verdict && verdict.error) {
    return { error: verdict.error, stage: "Judgment", partial: currentSolution };
  }

  return verdict;
}
