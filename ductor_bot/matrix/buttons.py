"""Button replacement for Matrix.

Matrix doesn't have inline keyboard buttons like Telegram.
Instead we render buttons as a numbered list and match numeric
replies back to the original label.
"""

from __future__ import annotations

import re


# Regex for [button:Label] markers (same as in formatting.py)
_BUTTON_RE = re.compile(r"\[button:([^\]]+)\]")


class ButtonTracker:
    """Per-room numbered option tracking for button replacement."""

    def __init__(self) -> None:
        self._active: dict[str, list[str]] = {}  # room_id → [label, ...]

    def extract_and_format(self, room_id: str, text: str) -> str:
        """Extract [button:...] markers, replace with numbered list.

        Returns the modified text with buttons replaced by a numbered list.
        If no buttons are found, returns text unchanged.
        """
        buttons: list[str] = _BUTTON_RE.findall(text)
        if not buttons:
            return text

        cleaned = _BUTTON_RE.sub("", text).rstrip()
        self._active[room_id] = buttons
        numbered = "\n".join(f"  {i + 1}. {label}" for i, label in enumerate(buttons))
        return f"{cleaned}\n\n{numbered}"

    def match_input(self, room_id: str, text: str) -> str | None:
        """If text is a number matching an active button, return the label."""
        choices = self._active.get(room_id, [])
        if not choices:
            return None
        try:
            idx = int(text.strip()) - 1
            if 0 <= idx < len(choices):
                label = choices[idx]
                del self._active[room_id]  # consume buttons
                return label
        except ValueError:
            pass
        return None

    def clear(self, room_id: str) -> None:
        """Clear active buttons for a room."""
        self._active.pop(room_id, None)
