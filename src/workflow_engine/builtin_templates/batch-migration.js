/**
 * batch-migration — Batch file migration workflow.
 *
 * Processes a list of files through a transformation pipeline:
 * analyze → transform → verify. Supports configurable batch size
 * and parallel processing within each batch.
 */

export const meta = {
  name: "batch-migration",
  description: "Batch file transformation with analysis, migration, and verification",
  phases: [
    { title: "Discovery", detail: "Identify files matching migration criteria" },
    { title: "Migration", detail: "Apply transformations in parallel batches" },
    { title: "Verification", detail: "Verify each transformed file" }
  ],
  maxConcurrent: 6,
  tools: ["claude", "coco", "aiden"]
};

export default async function main(args = {}) {
  const pattern = args.pattern || "**/*.js";
  const instruction = args.instruction || "Modernize the code";
  const batchSize = args.batchSize || 5;

  // Phase 1: Discovery
  phase("Discovery");
  log(`Discovering files matching: ${pattern}`);

  const discovery = await agent({
    prompt: `Find all files matching the glob pattern "${pattern}" in the current project.
For each file, assess whether it needs the following transformation: "${instruction}"

Output JSON:
{
  "files": [{ "path": "", "needs_migration": true, "reason": "" }],
  "total_found": 0,
  "needs_migration": 0
}`,
    tool: "claude",
    schema: { files: [], total_found: 0, needs_migration: 0 },
    label: "file-discovery",
  });

  const targets = (discovery.files || []).filter(f => f.needs_migration);
  log(`Found ${targets.length} files needing migration out of ${discovery.total_found} total`);

  if (targets.length === 0) {
    return "No files need migration. All files already conform to the target pattern.";
  }

  // Phase 2: Migration (batched)
  phase("Migration");

  const results = [];
  for (let i = 0; i < targets.length; i += batchSize) {
    const batch = targets.slice(i, i + batchSize);
    const batchNum = Math.floor(i / batchSize) + 1;
    const totalBatches = Math.ceil(targets.length / batchSize);
    log(`Processing batch ${batchNum}/${totalBatches} (${batch.length} files)`);

    // Check budget before each batch
    if (budget.remaining() < budget.total * 0.1) {
      log("Budget running low — stopping after current batch");
      break;
    }

    const batchResults = await parallel(
      batch.map((file, idx) => ({
        prompt: `Apply the following transformation to the file "${file.path}":

Instruction: ${instruction}
Reason this file was selected: ${file.reason}

Read the file, apply the transformation, and write the result back.
Then output JSON:
{
  "path": "${file.path}",
  "status": "success" or "skipped" or "failed",
  "changes_made": "brief description of changes",
  "lines_changed": 0
}`,
        tool: "coco",
        schema: { path: "", status: "", changes_made: "", lines_changed: 0 },
        label: `migrate-${file.path}`,
      }))
    );

    results.push(...batchResults);
  }

  // Phase 3: Verification
  phase("Verification");

  const successful = results.filter(r => r?.status === "success");
  log(`Verifying ${successful.length} migrated files`);

  if (successful.length === 0) {
    return "Migration completed but no files were successfully transformed.";
  }

  // Sample verification (verify up to 5 files to save tokens)
  const sampled = successful.slice(0, 5);
  const verifications = await parallel(
    sampled.map(file => ({
      prompt: `Verify the migration of "${file.path}".
Check that:
1. The file still compiles/parses correctly
2. The transformation "${instruction}" was applied correctly
3. No unintended side effects were introduced

Output JSON: { "path": "${file.path}", "valid": true/false, "issues": [] }`,
      tool: "aiden",
      schema: { path: "", valid: true, issues: [] },
      label: `verify-${file.path}`,
    }))
  );

  const invalid = verifications.filter(v => !v?.valid);
  const summary = `Migration complete:
- Files processed: ${results.length}
- Successful: ${successful.length}
- Failed: ${results.filter(r => r?.status === "failed").length}
- Skipped: ${results.filter(r => r?.status === "skipped").length}
- Verification issues: ${invalid.length}/${sampled.length} sampled`;

  log(summary);
  return summary;
}
