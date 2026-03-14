"""Shared formatting primitives for command response text."""

from __future__ import annotations

SEP = "\u2500\u2500\u2500"


def fmt(*blocks: str) -> str:
    """Join non-empty blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


# -- Shared response texts (eliminate duplication between handlers.py / commands.py) --

SESSION_ERROR_TEXT = fmt(
    "**Session Error**",
    SEP,
    "[{model}] An error occurred.\n"
    "Your session has been preserved -- send another message to retry.\n"
    "Use /new to start a fresh session if the problem persists.",
)

# Known CLI error patterns -> user-friendly short explanation.
_AUTH_PATTERNS = (
    "401",
    "unauthorized",
    "authentication",
    "signing in again",
    "sign in again",
    "token has been",
)
_RATE_PATTERNS = ("429", "rate limit", "too many requests", "quota exceeded")
_CONTEXT_PATTERNS = ("context length", "token limit", "maximum context", "too long")


def classify_cli_error(raw: str) -> str | None:
    """Return a user-facing hint for known CLI error patterns, or None."""
    lower = raw.lower()
    if any(p in lower for p in _AUTH_PATTERNS):
        return "Authentication failed. Please re-authenticate the CLI (e.g. `codex auth` or check your API key)."
    if any(p in lower for p in _RATE_PATTERNS):
        return "Rate limit reached. Wait a moment and try again."
    if any(p in lower for p in _CONTEXT_PATTERNS):
        return "Context length exceeded. Use /new to start a fresh session."
    return None


def session_error_text(model: str, cli_detail: str = "") -> str:
    """Build the error message shown to the user on CLI failure."""
    base = SESSION_ERROR_TEXT.format(model=model)
    hint = classify_cli_error(cli_detail) if cli_detail else None
    if hint:
        return fmt(base, f"**Cause:** {hint}")
    if cli_detail:
        # Show first meaningful line, truncated.
        detail = cli_detail.strip().split("\n")[0][:200]
        return fmt(base, f"**Detail:** `{detail}`")
    return base


TIMEOUT_ERROR_TEXT = fmt(
    "**Timeout**",
    SEP,
    "[{model}] CLI was terminated after {minutes} min.\n"
    "Your session has been preserved -- send another message to continue where it left off.\n"
    "Use /new to start a fresh session.",
)


def timeout_error_text(model: str, timeout_seconds: float) -> str:
    """Build the error message shown when the CLI times out."""
    minutes = int(timeout_seconds / 60)
    return TIMEOUT_ERROR_TEXT.format(model=model, minutes=minutes)


def new_session_text(provider: str) -> str:
    """Build /new response for provider-local reset."""
    provider_label = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}.get(
        provider.lower(), provider
    )
    return fmt(
        "**Session Reset**",
        SEP,
        f"Session reset for {provider_label} in this chat only.\n"
        "Other provider sessions were preserved.\n"
        "Send a message to continue.",
    )


def stop_text(killed: bool, provider: str) -> str:
    """Build the /stop response."""
    if killed:
        body = f"{provider} terminated. All queued messages discarded."
    else:
        body = "Nothing running right now."
    return fmt("**Agent Stopped**", SEP, body)


# -- Timeout messages --


def timeout_warning_text(remaining: float) -> str:
    """Warning text shown when a timeout is approaching."""
    if remaining >= 60:
        mins = int(remaining // 60)
        return f"Timeout in {mins} min"
    secs = int(remaining)
    return f"Timeout in {secs}s"


def timeout_extended_text(extension: float, remaining_ext: int) -> str:
    """Notification that the timeout was extended due to activity."""
    secs = int(extension)
    return f"Timeout extended (+{secs}s, {remaining_ext} left)"


def timeout_result_text(elapsed: float, configured: float) -> str:
    """Error text when a CLI process hit its timeout."""
    return fmt(
        "**Timeout**",
        SEP,
        f"The agent timed out after {int(elapsed)}s (limit: {int(configured)}s).\n"
        "Try a shorter prompt, or ask in smaller steps.",
    )


# -- Startup lifecycle messages --


def startup_notification_text(kind: str) -> str:
    """Notification text for startup events.

    Only ``first_start`` and ``system_reboot`` produce output.
    ``service_restart`` is silent (handled by the existing sentinel system).
    """
    if kind == "first_start":
        return fmt("**Bot Started**", SEP, "First start — ready to go.")
    if kind == "system_reboot":
        return fmt("**Bot Started**", SEP, "System reboot detected — back online.")
    return ""


# -- Auto-recovery messages --


def recovery_notification_text(
    kind: str,
    prompt_preview: str,
    session_name: str = "",
) -> str:
    """Notification that interrupted work is being recovered."""
    preview = prompt_preview[:80] + ("…" if len(prompt_preview) > 80 else "")
def format_technical_footer(
    model_name: str,
    total_tokens: int,
    input_tokens: int,
    cost_usd: float,
    duration_ms: float | None,
) -> str:
    """Format technical metadata as a footer line."""
    output_tokens = total_tokens - input_tokens
    parts = [f"Model: {model_name}"]
    parts.append(f"Tokens: {total_tokens} (in: {input_tokens}, out: {output_tokens})")
    if cost_usd > 0:
        parts.append(f"Cost: ${cost_usd:.4f}")
    if duration_ms is not None:
        secs = duration_ms / 1000
        parts.append(f"Time: {secs:.1f}s")
    return "\n---\n" + " | ".join(parts)


    if kind == "named_session":
        return fmt(
            "**Auto-Recovery**",
            SEP,
            f"Resuming background session **{session_name}**\n`{preview}`",
        )
    return fmt(
        "**Interrupted Task**",
        SEP,
        f"A task was interrupted during restart.\n`{preview}`\nPlease re-send to continue.",
    )
