"""Prompt assembly and reply shaping.

The gate decides WHETHER the interviewer speaks; this module only
handles HOW. The persona is re-injected into the system prompt on
every single call — nothing conversational accumulates outside the
transcript — and the reply is clamped after the fact: fenced blocks
stripped, sentence count capped, banned phrases dropped. If nothing
survives shaping, a deterministic fallback line from the pack is used
instead, so a weak or absent model can never break the session.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Unclosed think/fence blocks (a reply truncated mid-generation) are
# stripped to end-of-text: dropping tail content beats leaking it.
# A fence only counts when its ``` starts a line — an inline stray
# triple-backtick in prose is a marker to drop, not a block to gut.
_THINK_RX = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)
_FENCE_RX = re.compile(r"^[ \t]*```.*?(?:```|\Z)", re.DOTALL | re.MULTILINE)
_LABEL_RX = re.compile(r"^\s*(interviewer|assistant|me|reply|response)\s*:\s*", re.IGNORECASE)
# A sentence ends at terminal punctuation followed by whitespace, or at
# !/? glued to a capital ("Superb!You…"). A period glued to a capital is
# NOT a boundary — dotted names like pathlib.Path must survive whole.
_SENTENCE_END_RX = re.compile(r"(?<=[.!?])\s+|(?<=[!?])(?=[A-Z])")
# Fragments that end mid-abbreviation are glue points, not sentence ends.
_ABBREV_TAIL_RX = re.compile(r"(?:^|\s)(?:e\.g\.|i\.e\.|etc\.|vs\.|cf\.|[0-9]{1,2}\.|[A-Za-z]\.)$")
# A finished sentence ends in terminal punctuation (closing quotes or
# brackets may trail it). Anything else is a mid-generation cut.
_TERMINAL_RX = re.compile(r"[.!?…][\"')\]]*$")


def split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENTENCE_END_RX.split(text.strip()) if p and p.strip()]
    merged: List[str] = []
    for part in parts:
        if merged and _ABBREV_TAIL_RX.search(merged[-1]):
            merged[-1] += " " + part
        else:
            merged.append(part)
    return merged


def _clock_line(elapsed_s: float, remaining_s: float) -> str:
    def fmt(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return "%d:%02d" % divmod(seconds, 60)

    return "Clock: %s elapsed, %s remaining." % (fmt(elapsed_s), fmt(remaining_s))


def build_call(
    pack: Any,
    *,
    hint_level: int,
    rule_prompt: str,
    task: Optional[Dict[str, Any]],
    chat_tail: List[Dict[str, str]],
    snapshots: Dict[str, str],
    recent_runs: List[Dict[str, Any]],
    elapsed_s: float,
    remaining_s: float,
    prev_task: Optional[Dict[str, Any]] = None,
    task_pos: Optional[Tuple[int, int]] = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """Build (system, messages) for one adapter call. Everything is
    reassembled from scratch each time — persona drift has nowhere to
    accumulate."""

    system_parts: List[str] = [pack.persona.strip()]
    system_parts.append(
        "Hard limit: reply with at most %d sentence%s and no more. "
        "Plain prose only — never any fenced block."
        % (pack.max_sentences, "" if pack.max_sentences == 1 else "s")
    )
    if task is not None:
        system_parts.append("The task on their screen:\n%s" % task["statement"])
        if pack.include_task_notes and task.get("notes"):
            system_parts.append("Interviewer-only notes for this task:\n%s" % task["notes"])
    if prev_task is not None and pack.include_previous_task:
        prev = "The PREVIOUS task, already finished (they may still ask about it):\n%s" % prev_task["statement"]
        if pack.include_task_notes and prev_task.get("notes"):
            prev += "\n\nYour notes for it:\n%s" % prev_task["notes"]
        system_parts.append(prev)
    if hint_level > 0 and pack.ladder:
        step = pack.ladder[min(hint_level, len(pack.ladder)) - 1]
        system_parts.append("Hint level %d — %s" % (hint_level, step))
    if rule_prompt:
        system_parts.append("Right now: %s" % rule_prompt)
    system = "\n\n".join(part for part in system_parts if part)

    messages: List[Dict[str, str]] = []
    tail = chat_tail[-pack.recent_messages :] if pack.recent_messages > 0 else []
    for row in tail:
        role = "assistant" if row["kind"] == "interviewer_message" else "user"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + row["text"]
        else:
            messages.append({"role": role, "content": row["text"]})

    context_parts: List[str] = []
    if pack.include_clock:
        line = _clock_line(elapsed_s, remaining_s)
        if task_pos is not None:
            line += " Task %d of %d." % task_pos
        context_parts.append(line)
    if pack.show_latest_snapshot:
        for path, content in snapshots.items():
            body = content
            if len(body) > pack.snapshot_max_chars:
                body = body[-pack.snapshot_max_chars :]
                body = "[start truncated]\n" + body
            context_parts.append("On screen — %s as last saved:\n%s" % (path, body))
    for run in recent_runs[-pack.recent_runs :] if pack.recent_runs > 0 else []:
        out = (run.get("out") or "").strip()
        err = (run.get("err") or "").strip()
        merged = "\n".join(p for p in (out, err) if p)
        if len(merged) > pack.run_output_max_chars:
            merged = merged[: pack.run_output_max_chars] + "\n[truncated]"
        context_parts.append(
            "They ran: %s\nExit status %s. Output:\n%s"
            % (run.get("cmd", ""), run.get("exit_status"), merged or "(none)")
        )
    context_parts.append(
        "Respond now, in persona, at most %d sentence%s."
        % (pack.max_sentences, "" if pack.max_sentences == 1 else "s")
    )
    context = "\n\n".join(context_parts)

    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n\n[" + context + "]"
    else:
        messages.append({"role": "user", "content": "[" + context + "]"})
    return system, messages


def shape_reply(text: str, pack: Any, fallback_i: int) -> Tuple[str, bool, int]:
    """Clamp a raw model reply to the pack's limits.

    Returns (final_text, used_fallback, next_fallback_i). Deterministic
    given (text, pack, fallback_i).
    """
    cleaned = _THINK_RX.sub(" ", text or "")
    if pack.strip_fenced_blocks:
        cleaned = _FENCE_RX.sub(" ", cleaned)
        cleaned = cleaned.replace("```", " ")  # inline stray markers
    cleaned = _LABEL_RX.sub("", cleaned.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    sentences = split_sentences(cleaned)
    # word-boundary match: "love it" must not condemn "love itertools"
    banned = [
        re.compile(r"(?<![a-z0-9])" + re.escape(b.casefold()).replace(r"\ ", " ").replace(" ", r"\s+") + r"(?![a-z0-9])")
        for b in pack.banned_phrases
    ]
    kept: List[str] = []
    for sentence in sentences:
        low = sentence.casefold()
        if any(rx.search(low) for rx in banned):
            continue
        kept.append(sentence)
        if len(kept) >= pack.max_sentences:
            break

    # a reply the endpoint cut off mid-generation (max_tokens ran out)
    # ends without terminal punctuation: drop the dangling fragment, or
    # mark the cut when the fragment is all we have — never present a
    # mid-sentence cut as if it were the intended reply
    if kept and not _TERMINAL_RX.search(kept[-1]):
        if len(kept) > 1:
            kept.pop()
        else:
            kept[-1] += " …"

    final = " ".join(kept).strip()
    # over the char budget: drop whole trailing sentences, never cut
    # mid-thought — a clipped explanation is worse than a shorter one
    while len(final) > pack.max_chars and len(kept) > 1:
        kept.pop()
        final = " ".join(kept).strip()
    if len(final) > pack.max_chars:
        head = final[: max(pack.max_chars - 2, 0)]
        cut = head.rsplit(" ", 1)[0].rstrip()
        final = (cut + " …") if cut else head.rstrip()

    if final:
        return final, False, fallback_i
    lines = pack.fallback_lines
    line = lines[fallback_i % len(lines)]
    return line, True, fallback_i + 1
