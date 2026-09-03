"""The Loop protocol text and the detection of its completion/needs-input markers.

Invariants:
- The protocol text appended to every prompt and the parser that reads its markers
  back out live together so the contract and its detection can never drift apart.
- The protocol is built once per run from the configured interactive-only label
  and the concrete children Ralph resolved as carrying it (``build_protocol``)
  rather than being a bare constant, so the label rule, the resolved facts, and the
  markers they reference stay in this one module. The built text is published for
  the whole run through ``set_active_protocol`` and read back by the backends
  through ``active_protocol`` -- import the *functions*, never the
  ``_ACTIVE_PROTOCOL`` global, exactly as ``redaction`` is used: a caller that
  captured the old binding would keep appending a stale label. ``active_protocol``
  always holds the default label's protocol until the loop overrides it -- once the
  trust boundary is proven and the concrete blocked children are resolved, before
  the first session -- so it is never unset.
- The protocol signals *progress* as well as outcome (register G6). Alongside the
  completion and needs-input markers it asks the Backend to declare its Stage --
  which part of the operator's own prompt it has reached -- as the exact standalone
  line ``<promise>STAGE: label</promise>``, read back by ``extract_stage``. The
  label is free text in the Backend's own wording, because the stages belong to the
  prompt, which Ralph snapshots but never reads; no enumeration here could name
  them, and a fixed vocabulary would force an unusual workflow to distort itself to
  fit. The protocol offers ``SUGGESTED_STAGES`` as an example to shape the wording,
  never as a set to map onto (the ratified owner decision on issue #44). Free text
  is therefore bounded and sanitized where it is parsed, never where it is shown.
  A Stage decides no outcome: it is progress, and the parser keeps it that way.
- The widened protocol does not disturb the signalling it was widened from. A Stage
  line is not the completion marker and is not the needs-input marker, and
  ``visible_prose_lines`` blanks it, so a Stage declared after a concluding question
  cannot hide that question from the heuristic and one declared after an explicit
  needs-input marker is never read back as part of the question.
- Marker detection reads only *visible* Markdown: fenced code, indented code, and
  block quotes are excluded so a ``<promise>...</promise>`` line quoted inside the
  prompt, a code sample, or tool output is never mistaken for an iteration result.
- The marker vocabulary has one spelling here. ``COMPLETION_MARKER``,
  ``NEEDS_INPUT_MARKER`` and ``STAGE_MARKER`` are named once and read by every
  parser, by ``is_marker_declaration`` -- which answers whether a single line is a
  declaration at all -- and by ``without_marker_lines``, which drops those
  declarations from a message bound for an operator. A marker is Ralph's own
  contract echoed back rather than the Backend's prose, and the outcome reported
  beside a concluding message already states what a marker signalled, so the
  display shows the prose (the ratified owner decision on issue #58). What counts
  as a declaration is the same line-anchored match everywhere, so the console can
  never drop something a parser still reads as prose, or keep something it reads
  as a signal.
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
has_completion_marker plus explicit_needs_input / inferred_needs_input, streaming
text to extract_stage as the session speaks, tool payloads to extract_question,
and the withdrawn/unmarked fragments through ``bounded_quote`` before warning on
them), ``loop`` (passes a concluding message through ``without_marker_lines`` on
its way to the Run console, which owns no marker knowledge of its own).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


# The label that marks a child issue as reserved for an interactive operator
# session. Repositories already using this convention need no configuration; a
# repository using a different one names its own through ``ralph run``.
DEFAULT_INTERACTIVE_LABEL = "may-ask-owner"

# The wording the protocol offers as an example. It is guidance the Backend may
# deviate from, not an enumeration it must map onto (the ratified owner decision on
# issue #44); it lives here so the suggestion and the parser that accepts anything
# stay in the one module.
SUGGESTED_STAGES = ("selecting", "loading context", "implementing", "finishing")


def build_protocol(
    interactive_label: str, interactive_only_issues: list[int] | None = None
) -> str:
    """Return the Loop protocol text for a run, naming ``interactive_label`` as the
    label that reserves a child for an interactive operator session. Built once per
    run so the label rule and the markers it references stay in this one module.

    ``interactive_only_issues`` are the concrete open children Ralph resolved as
    carrying the label (see ``gitcontext.interactive_only_issues``); their numbers
    are injected so the Backend is given the facts, not only the rule. ``None`` means
    the run has not resolved them yet -- the safe default before the trust boundary
    is proven -- and omits the concrete line; a list (including an empty one) states
    the resolved children explicitly, an empty result set as such rather than
    omitted. The injected list is advisory: Ralph cannot observe which child the
    Backend selects, so only the resulting needs-input halt is enforced."""
    if interactive_only_issues is None:
        resolved = ""
    elif interactive_only_issues:
        rendered = ", ".join(f"#{number}" for number in interactive_only_issues)
        resolved = (
            f"\n- Resolved at the start of this run, the open children carrying "
            f"`{interactive_label}` are: {rendered}. Treat exactly these as blocked "
            f"for this iteration and do not select any of them. This resolved list "
            f"is advisory -- Ralph cannot observe which child you select -- so honour "
            f"it; only the resulting needs-input halt is enforced."
        )
    else:
        resolved = (
            f"\n- Resolved at the start of this run, no open child currently carries "
            f"`{interactive_label}`, so none is blocked on that basis."
        )
    suggested = ", ".join(SUGGESTED_STAGES[:-1]) + f", or {SUGGESTED_STAGES[-1]}"
    return f"""

Ralph loop protocol:
- Implement at most one child issue in this iteration.
- A child issue labelled `{interactive_label}` is reserved for an interactive
  session with the operator and is blocked for this iteration: do not select it,
  and never guess the decision it embeds. Select the next unblocked child that
  does not carry `{interactive_label}` instead.{resolved}
- When every remaining actionable child is labelled `{interactive_label}`, do not
  emit <promise>COMPLETE</promise> -- work remains and is waiting on the operator.
  Emit <promise>NEEDS_INPUT</promise> naming those `{interactive_label}` children
  so the operator knows which ones need an interactive session.
- Finishing that one child while unblocked children still remain is a normal
  end of iteration -- not completion and not a question. Emit no marker, do not
  ask whether to continue, and stop. The next iteration independently selects
  the next unblocked child, so "should I proceed with the next child?" is always
  answerable from the issue tracker and this protocol and is never operator input.
- Emit the completion marker when no unfinished child remains or when every
  remaining child has explicit blocker evidence such as a declared dependency,
  blocker label, or clear prerequisite state.
- As you work, declare which stage of the supplied prompt's own workflow you are
  in, so the operator can see live what you are doing. Emit the exact standalone
  line <promise>STAGE: label</promise> when you enter a stage and again whenever
  it changes, where `label` is a few words in your own wording -- for example
  {suggested}. Those examples are a
  suggestion to shape the wording, not a fixed vocabulary to map onto: describe
  the stages the supplied prompt actually has. Keep a label to a handful of
  words; a longer one is shortened for display. A stage declaration reports
  progress and is never an iteration result -- it neither completes the iteration
  nor halts it.
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


# The protocol built for the current run, published here for the whole process so
# the backends append the run's configured label. Defaults to the built-in label's
# protocol so it is never unset; the loop overrides it once per run, after preflight
# proves gh and the concrete blocked children are resolved. Import
# ``active_protocol`` / ``set_active_protocol``, never this global (see Invariants).
_ACTIVE_PROTOCOL = build_protocol(DEFAULT_INTERACTIVE_LABEL)


def active_protocol() -> str:
    return _ACTIVE_PROTOCOL


def set_active_protocol(protocol: str) -> None:
    global _ACTIVE_PROTOCOL
    _ACTIVE_PROTOCOL = protocol


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


# The two outcome markers, named once so the parsers that read them and the filter
# that removes them from displayed prose cannot drift apart.
COMPLETION_MARKER = "<promise>COMPLETE</promise>"
NEEDS_INPUT_MARKER = "<promise>NEEDS_INPUT</promise>"

# The Stage marker the widened protocol asks the Backend to declare (register G6).
# It shares the ``<promise>`` envelope of the outcome markers so the prompt carries one
# marker vocabulary, and a distinct verb so it can never be confused with them: a Stage
# line is progress and decides no outcome.
STAGE_MARKER = re.compile(r"<promise>STAGE:(.*)</promise>")


def stage_declaration(line: str) -> str | None:
    """Return the raw label of the Stage declaration *line* is, or ``None`` when it is
    not one. One definition of the shape, read by both the parser and the prose filter,
    so an indented or malformed declaration cannot be a declaration to one of them and
    prose to the other. Matched against the whole line exactly as the outcome markers
    are: a stage mentioned inside a sentence is prose, not a declaration."""
    match = STAGE_MARKER.fullmatch(line)
    return None if match is None else match.group(1)


def is_marker_declaration(line: str) -> bool:
    """Whether *line* is one of the protocol's own marker declarations -- a Stage, a
    completion, or a needs-input marker. Matched exactly as each parser above matches
    it, so what the console drops from displayed prose is precisely what the parsers
    read as a signal: a marker mentioned inside a sentence is prose to all of them.

    A predicate, unlike ``stage_declaration`` beside it, which hands back the label it
    found: the ``is_`` says so, because which of the three a line is does not matter to
    the only caller -- what matters is that it is not prose."""
    return (
        stage_declaration(line) is not None
        or line == COMPLETION_MARKER
        or line == NEEDS_INPUT_MARKER
    )


def without_marker_lines(text: str) -> str:
    """*text* with the protocol's own marker declarations dropped and every other line
    byte-identical. The markers are Ralph's contract echoed back rather than the
    Backend's prose, so an operator-facing rendering of a concluding message shows the
    prose and lets the outcome beside it report what the markers signalled (the
    ratified owner decision on issue #58).

    Only declarations among the *visible* Markdown lines go, so a marker quoted inside
    a fenced block -- prose to every parser here -- stays prose to the display too.
    Callers strip before the Run console neutralises: whitespace controls become one
    space there, which would fuse a marker line into the sentence beside it and leave
    nothing for a line-anchored match to find."""
    declarations = {
        index for index, line in visible_markdown_lines(text) if is_marker_declaration(line)
    }
    return "\n".join(
        line for index, line in enumerate(text.splitlines()) if index not in declarations
    )


# The most of a declared Stage label that is kept. The label is free text -- the stages
# belong to the operator's prompt, which Ralph snapshots but never reads, so no fixed
# vocabulary could name them -- and free text needs a bound: it shares one status-line
# field with the tool name, and a Backend that answers with a sentence must not push
# every other field off the line.
STAGE_LABEL_LIMIT = 32
def extract_stage(text: str) -> str | None:
    """Return the Stage label most recently declared in *text*, or ``None`` when it
    declares none. Read from whatever the Backend has said so far rather than only
    from its final message, so the console can show a stage while the Iteration runs.

    The label is free text and is therefore bounded and sanitized here rather than
    trusted: whitespace is collapsed to one line, an empty label is refused, one
    carrying angle brackets or a control character (an escape sequence bound for a
    terminal) is refused outright, and one longer than ``STAGE_LABEL_LIMIT`` is
    shortened. A refused declaration is simply not a declaration -- the previous
    Stage stands and the status line falls back to the last tool once it goes stale
    -- so a malformed label can never be rendered raw. Redaction is the Run
    console's, at its single choke point (register G17)."""
    for _, line in reversed(visible_markdown_lines(text)):
        raw = stage_declaration(line)
        if raw is None:
            continue
        label = " ".join(raw.split())
        if not label or "<" in label or ">" in label:
            continue
        if any(unicodedata.category(character) in ("Cc", "Cf") for character in label):
            # Every control and format codepoint, not only the ASCII ones: the C1
            # block carries an 8-bit CSI a terminal would act on, and the bidirectional
            # overrides can make a label read as something other than what it says.
            continue
        if len(label) > STAGE_LABEL_LIMIT:
            label = label[: STAGE_LABEL_LIMIT - 3] + "..."
        return label
    return None


def has_completion_marker(text: str) -> bool:
    return any(line == COMPLETION_MARKER for _, line in visible_markdown_lines(text))


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
        if stage_declaration(line) is not None:
            # A Stage declaration is progress, not prose. Blanking it keeps the
            # widened protocol from disturbing the outcome signalling it now shares
            # a prompt with: a stage line trailing a concluding question must not
            # hide that question from the heuristic, and one following an explicit
            # needs-input marker must not be read back as part of the question.
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
        if line == NEEDS_INPUT_MARKER
    ]
    if not marker_indexes:
        return None
    marker_index = marker_indexes[-1]
    following = [line for index, line in visible if index > marker_index and line]
    return "\n".join(following) or "The assistant requested operator input."


# The most of a quoted marker fragment a mid-run warning prints. A withdrawn or
# unmarked question is surfaced as a bounded interruption, not as an outlet for the
# backend's whole final message: the operator needs enough to recognise the question,
# not the narration around it (issue #39).
MARKER_QUOTE_LIMIT = 200


def bounded_quote(text: str) -> str:
    """Collapse a quoted marker fragment to one line and cap its length, so the
    withdrawn-marker and unmarked-question warnings stay bounded interruptions rather
    than printing unbounded backend prose mid-stream (issue #39). Callers redact
    *before* bounding, so a secret is replaced whole and the placeholder is what gets
    measured; the retained stream keeps the fragment in full (register G18)."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= MARKER_QUOTE_LIMIT:
        return collapsed
    return collapsed[: MARKER_QUOTE_LIMIT - 3] + "..."


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
