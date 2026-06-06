/**
 * doc-generation — API and README documentation generation workflow.
 *
 * Discovers the project's API surface through architectural analysis, produces
 * README/API-docs/usage-examples in parallel, and validates the output.
 */

export const meta = {
  name: "doc-generation",
  description: "Discover API surface, generate README/API/usage docs in parallel, and validate",
  phases: [
    { title: "API Discovery", detail: "Architectural analysis to identify modules and public APIs" },
    { title: "Document Generation", detail: "Parallel generation of README, API docs, and usage examples" },
    { title: "Merge & Validate", detail: "Consolidate documents and validate schema compliance" }
  ],
  maxConcurrent: 8,
  tools: ["acp", "claude", "coco"]
};

export default async function main(args = {}) {
  const target = args.target || ".";
  const focus = args.focus || "";

  // Phase 1: API Discovery
  phase("API Discovery");
  log(`Analyzing project structure at: ${target}`);

  const structure = await agent({
    prompt: `You are a software architect. Analyze the project at "${target}" ${focus ? `(focus: ${focus})` : ""}.
Discover the API surface: public modules, exported functions/classes, key interfaces, and configuration points.

Please prefer using subagent workflows where possible.

Output JSON:
{
  "modules": [{ "name": "", "path": "", "public_apis": ["..."] }],
  "configuration_points": [],
  "entry_points": [],
  "architecture_summary": ""
}`,
    tool: "acp",
    role: "架构师",
    schema: {
      modules: [],
      configuration_points: [],
      entry_points: [],
      architecture_summary: "",
    },
    label: "api-discovery",
  });

  const moduleList = (structure.modules || [])
    .map(m => `${m.name || ""} (${m.path || ""})`)
    .join("\n");
  log(`Discovered ${(structure.modules || []).length} modules`);

  // Phase 2: Document Generation
  phase("Document Generation");
  log("Generating README, API docs, and usage examples in parallel");

  const docs = await parallel([
    {
      prompt: `You are a technical writer. Write a concise README for the project at "${target}".

Discovered modules:
${moduleList}

Architecture summary: ${structure.architecture_summary || ""}

Please prefer using subagent workflows where possible.

Output a well-structured markdown README that includes: project overview, quick start, installation, basic usage, and contribution notes.

Output JSON: { "title": "README", "content": "" }`,
      tool: "claude",
      role: "技术文档作者",
      schema: { title: "README", content: "" },
      label: "gen-readme",
      phase: "Document Generation",
    },
    {
      prompt: `You are an API documentation specialist. Generate API documentation for:

${moduleList}

Architecture summary: ${structure.architecture_summary || ""}

Please prefer using subagent workflows where possible.

Produce a structured markdown document listing each public API with: purpose, signature, parameters, return value, and a tiny example.

Output JSON: { "title": "API Reference", "content": "" }`,
      tool: "claude",
      role: "技术文档作者",
      schema: { title: "API Reference", content: "" },
      label: "gen-api-docs",
      phase: "Document Generation",
    },
    {
      prompt: `You are a developer advocate. Produce usage examples for:

${moduleList}

Architecture summary: ${structure.architecture_summary || ""}

Please prefer using subagent workflows where possible.

Focus on realistic, runnable examples — 3 to 5 common usage patterns.

Output JSON: { "title": "Usage Examples", "content": "" }`,
      tool: "claude",
      role: "开发布道者",
      schema: { title: "Usage Examples", content: "" },
      label: "gen-examples",
      phase: "Document Generation",
    },
  ]);

  log(`Generated ${docs.length} document sections`);

  // Phase 3: Merge & Validate
  phase("Merge & Validate");

  const merged = docs.map(d => `# ${d?.title || "section"}\n\n${d?.content || ""}\n`).join("\n---\n");

  const validation = await agent({
    prompt: `Validate and merge the generated documentation sections for "${target}".

Number of sections: ${docs.length}

Please prefer using subagent workflows where possible.

Produce a final JSON summary capturing:
- combined document metadata
- any cross-reference gaps to resolve
- suggested follow-up updates

Output JSON:
{
  "sections": [],
  "word_count": 0,
  "cross_reference_gaps": [],
  "recommendations": [],
  "merged_content": ""
}`,
    tool: "acp",
    schema: {
      sections: [],
      word_count: 0,
      cross_reference_gaps: [],
      recommendations: [],
      merged_content: "",
    },
    label: "doc-validation",
  });

  return {
    summary: validation,
    findings: structure.modules || [],
    phaseResults: { structure, docs, validation, merged },
  };
}
