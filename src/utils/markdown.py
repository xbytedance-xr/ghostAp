"""Markdown text utilities."""


def safe_truncate_markdown(
    content: str, max_length: int = 25000, keep_head: bool = True
) -> str:
    """带 Markdown 闭合保护的安全截断机制。

    防止因内容超长导致飞书卡片发送/更新失败，同时避免截断导致 markdown 标签（如代码块）未闭合而引起渲染错乱。
    """
    if not content or len(content) <= max_length:
        return content or ""

    notice = f"\n\n> ⚠️ 内容过长（超过 {max_length} 字符），已自动截断。"

    if keep_head:
        truncated = content[:max_length - len(notice)]
    else:
        truncated = content[-(max_length - len(notice)):]

    # 修复代码块闭合
    fence_count = truncated.count("```")
    if fence_count % 2 != 0:
        if keep_head:
            truncated += "\n```"
        else:
            truncated = "```\n" + truncated

    if keep_head:
        return truncated + notice
    else:
        return notice + "\n\n" + truncated
