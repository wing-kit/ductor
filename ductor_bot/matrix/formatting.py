"""Convert Markdown to Matrix-compatible HTML.

Matrix supports a richer subset of HTML than Telegram:
- Real headings (h1–h6)
- Code blocks with language hints
- No 4096-char limit (65KB per event)

Returns ``(plain_body, formatted_body)`` for ``m.room.message`` content.
"""

from __future__ import annotations

import html
import re

# Regex for [button:Label] markers
_BUTTON_RE = re.compile(r"\[button:([^\]]+)\]")


def strip_button_markers(text: str) -> str:
    """Remove ``[button:...]`` markers from text."""
    return _BUTTON_RE.sub("", text).rstrip()


def markdown_to_matrix_html(text: str) -> tuple[str, str]:
    """Convert Markdown to Matrix HTML.

    Returns (plain_body, formatted_body).
    """
    cleaned = strip_button_markers(text)
    formatted = _convert_markdown(cleaned)
    plain = _strip_html(formatted)
    return plain, formatted


def _convert_markdown(text: str) -> str:
    """Convert a subset of Markdown to HTML."""
    lines = text.split("\n")
    result: list[str] = []
    in_code_block = False
    code_lang = ""

    for line in lines:
        # Code block toggle
        if line.startswith("```"):
            if in_code_block:
                result.append("</code></pre>")
                in_code_block = False
            else:
                code_lang = line[3:].strip()
                lang_attr = f' class="language-{html.escape(code_lang)}"' if code_lang else ""
                result.append(f"<pre><code{lang_attr}>")
                in_code_block = True
            continue

        if in_code_block:
            result.append(html.escape(line))
            continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            content = _inline_format(m.group(2))
            result.append(f"<h{level}>{content}</h{level}>")
            continue

        # Horizontal rule
        if re.match(r"^---+$", line.strip()):
            result.append("<hr>")
            continue

        # Normal line with inline formatting
        result.append(_inline_format(line))

    if in_code_block:
        result.append("</code></pre>")

    return "\n".join(result)


def _inline_format(text: str) -> str:
    """Apply inline formatting: bold, italic, strikethrough, code, links."""
    # Escape HTML first (but preserve already-produced tags)
    text = html.escape(text)

    # Inline code (must be before bold/italic to avoid conflicts)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # Bold (**text** or __text__)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)

    # Italic (*text* or _text_)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", text)

    # Strikethrough (~~text~~)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)

    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    return text


def _strip_html(formatted: str) -> str:
    """Strip HTML tags to produce a plain-text body."""
    text = re.sub(r"<[^>]+>", "", formatted)
    return html.unescape(text)
