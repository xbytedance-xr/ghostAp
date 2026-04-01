import re

__all__ = ["COMPACT_SECTIONS", "build_compact_prompt", "extract_summary"]

COMPACT_SECTIONS = [
    "Primary Request",
    "Key Technical Concepts",
    "Files and Code Sections",
    "Errors and Fixes",
    "Problem Solving",
    "Pending Tasks",
    "Current Work",
]

COMPACT_PROMPT_TEMPLATE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
Tool calls will be REJECTED. Your entire response must be plain text.

Compress the following conversation into a structured summary.
Use ONLY these sections (skip empty ones):
{sections}

Rules:
- Be concise but preserve all technical details, file paths, and code references.
- Use bullet points within each section.
- Wrap your analysis in <analysis> tags, then output the final summary in <summary> tags.
- Do NOT use any tools. Text response ONLY.

Conversation:
{conversation}"""


def build_compact_prompt(conversation_history: list[dict]) -> str:
    sections = "\n".join(f"- {s}" for s in COMPACT_SECTIONS)
    lines: list[str] = []
    for msg in conversation_history:
        role = msg.get("role", "unknown")
        content = str(msg.get("content", ""))
        lines.append(f"[{role}]: {content}")
    conversation = "\n".join(lines)
    return COMPACT_PROMPT_TEMPLATE.format(sections=sections, conversation=conversation)


def extract_summary(llm_response: str) -> str:
    match = re.search(r"<summary>(.*?)</summary>", llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    cleaned = re.sub(r"<analysis>.*?</analysis>", "", llm_response, flags=re.DOTALL)
    return cleaned.strip()
