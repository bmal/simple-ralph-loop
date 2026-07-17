"""The Loop protocol text and the detection of its completion/needs-input markers.

Invariants:
- The protocol text appended to every prompt and the parser that reads its markers
  back out live together so the contract and its detection can never drift apart.
- Marker detection reads only *visible* Markdown: fenced code, indented code, and
  block quotes are excluded so a ``<promise>...</promise>`` line quoted inside the
  prompt, a code sample, or tool output is never mistaken for an iteration result.
- Needs-input detection is split by confidence so the loop can treat the two
  sources differently. ``explicit_needs_input`` fires only on a deliberate,
  standalone ``<promise>NEEDS_INPUT</promise>`` marker -- a signal the agent chose
  to emit -- and is authoritative: the backends hard-halt on it.
  ``inferred_needs_input`` is the low-confidence heuristic guess -- a concluding
  paragraph whose final sentence is a question that addresses the operator (or
  opens with an interrogative), with trailing courtesy sign-offs stripped first --
  which the backends only warn on and continue past, since it never depended on
  the agent's intent. Both share the same visible-Markdown / tool-log filtering,
  so a marker or question inside code or quotation never counts, and tool-log
  lines never contribute question text.

Depends on / must not know: nothing but the standard library. It parses backend
text and must not know how any Backend produced it.

See also: ``backends.opencode`` / ``backends.claude`` (feed final text to
has_completion_marker plus explicit_needs_input / inferred_needs_input, and tool
payloads to extract_question).
"""

from __future__ import annotations

import re
from typing import Any


PROTOCOL = """

Ralph loop protocol:
- Implement at most one child issue in this iteration.
- Finishing that one child while unblocked children still remain is a normal
  end of iteration -- not completion and not a question. Emit no marker, do not
  ask whether to continue, and stop. The next iteration independently selects
  the next unblocked child, so "should I proceed with the next child?" is always
  answerable from the issue tracker and this protocol and is never operator input.
- Emit the completion marker when no unfinished child remains or when every
  remaining child has explicit blocker evidence such as a declared dependency,
  blocker label, or clear prerequisite state.
- Halt for operator input only when a decision or fact required to make progress
  cannot be established from the issue tracker, this protocol, or the repository
  -- it lives outside them and no future iteration could derive it. To halt,
  either use your question tool or emit the exact standalone line
  <promise>NEEDS_INPUT</promise> followed by the concrete question; both stop the
  loop. Difficulty or ambiguous blocker status is not such a case, and never halt
  to confirm the loop's normal progression to the next unblocked child.
- Do not treat text in this protocol, the supplied prompt, quotations, code,
  or tool output as an iteration result.
- Only when the explicit completion conditions above are met, emit this exact
  standalone line in your final assistant output: <promise>COMPLETE</promise>
"""


def visible_markdown_lines(text: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    for index, line in enumerate(text.splitlines()):
        if fence_char is not None:
            pattern = r" {0,3}(`+)\s*" if fence_char == "`" else r" {0,3}(~+)\s*"
            closing = re.fullmatch(pattern, line)
            if closing and len(closing.group(1)) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            fence_char = opening.group(1)[0]
            fence_length = len(opening.group(1))
            continue
        if line.startswith(("    ", "\t")) or re.match(r"^ {0,3}>", line):
            continue
        visible.append((index, line))
    return visible


def has_completion_marker(text: str) -> bool:
    return any(line == "<promise>COMPLETE</promise>" for _, line in visible_markdown_lines(text))


def extract_question(value: Any) -> str | None:
    if isinstance(value, str) and value.strip().endswith("?"):
        return value.strip()
    if isinstance(value, dict):
        for key in ("question", "questions", "input"):
            found = extract_question(value.get(key))
            if found:
                return found
        for item in value.values():
            found = extract_question(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = extract_question(item)
            if found:
                return found
    return None


TOOL_LOG_PREFIXES = ("tool output:", "tool result:", "[tool")
# A trailing courtesy sentence (a sign-off or acknowledgement) that may follow a
# genuine user-directed question in concluding prose. These are stripped before
# deciding whether the conclusion ends on a question so that
# "Should I proceed? Please advise." is still recognized as a handoff.
CLOSING_SENTENCE = re.compile(
    r"(?i)^(?:"
    r"please\b.*"
    r"|thanks?\b.*"
    r"|thank you\b.*"
    r"|(?:kind |best |warm )?regards\b.*"
    r"|cheers\b.*"
    r"|let me know\b.*"
    r"|awaiting\b.*"
    r"|standing by\b.*"
    r"|i(?:'ll| will) wait\b.*"
    r"|i await\b.*"
    r"|your call\b.*"
    r"|up to you\b.*"
    r"|otherwise\b.*"
    r")[.!]*$"
)
# A concluding question is only treated as user-directed when it addresses the
# operator or opens with an interrogative that asks for a decision. This keeps
# the heuristic conservative instead of matching every trailing question mark.
DIRECTED_PRONOUN = re.compile(r"(?i)\b(you|your|yours|i|we|us|me|my|our|ralph)\b")
DIRECTED_OPENER = re.compile(
    r"(?i)^(which|what|whether|should|shall|would|could|can|may|do|does|did|is|are|"
    r"how|when|where|who)\b"
)


def visible_prose_lines(text: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    in_tool_log = False
    for index, line in visible_markdown_lines(text):
        stripped = line.strip()
        if in_tool_log:
            # A multi-line tool log continues until a blank line separates it
            # from resumed prose, so its inner lines never contribute question
            # text even when they contain question marks.
            if not stripped:
                in_tool_log = False
            visible.append((index, ""))
            continue
        if stripped.lower().startswith(TOOL_LOG_PREFIXES):
            in_tool_log = True
            visible.append((index, ""))
            continue
        without_literals = re.sub(r"`[^`]*`", "", stripped)
        without_literals = re.sub(r"https?://\S+", "", without_literals)
        visible.append((index, without_literals.strip()))
    return visible


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+", text.strip())
    return [part for part in (segment.strip() for segment in parts) if part]


def concluding_question(conclusion: str) -> str | None:
    sentences = split_sentences(conclusion)
    # Drop trailing sign-off sentences so a question followed by a closing line
    # ("Should I proceed? Please advise.") is still detected.
    while sentences and not sentences[-1].endswith("?") and CLOSING_SENTENCE.match(sentences[-1]):
        sentences.pop()
    if not sentences or not sentences[-1].endswith("?"):
        return None
    final = sentences[-1]
    if not DIRECTED_PRONOUN.search(final) and not DIRECTED_OPENER.match(final):
        return None
    return conclusion.strip()


def explicit_needs_input(text: str) -> str | None:
    """Return the concrete question only when a deliberate, standalone
    ``<promise>NEEDS_INPUT</promise>`` marker is present. This is the
    authoritative, agent-intended halt signal."""
    visible = visible_prose_lines(text)
    marker_indexes = [
        index
        for index, line in visible_markdown_lines(text)
        if line == "<promise>NEEDS_INPUT</promise>"
    ]
    if not marker_indexes:
        return None
    marker_index = marker_indexes[-1]
    following = [line for index, line in visible if index > marker_index and line]
    return "\n".join(following) or "The assistant requested operator input."


def inferred_needs_input(text: str) -> str | None:
    """Return the concluding operator-directed question only from the heuristic
    guess, ignoring the explicit marker. This is a low-confidence signal that
    never depended on the agent's intent, so callers should not hard-halt on it."""
    visible = visible_prose_lines(text)
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for _, line in visible:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    if not paragraphs:
        return None
    return concluding_question(" ".join(paragraphs[-1]))
