#!/usr/bin/env python3
"""Surface the shape of what lands in a markdown file, whatever wrote it.

A document is read by someone who never saw the version before it. Prose
written while making a change describes the change instead -- "now uses",
"previously", a `## Recent changes` heading -- and to that reader it is noise
that outlives the change it describes. Appending is also how a document goes
stale: the section that should have been replaced is still there, one screen
further up, and now two things claim to be true.

Comments fail the same way and answer to the same rule, but not to the same
reading. A comment is a minority of the lines in a file and can be listed back
one at a time; a markdown file is prose throughout, so listing what landed only
echoes the edit. What carries signal here is the shape of the edit: how much it
added against how much it replaced, how large the file has become, and whether
the new sentences describe a transition rather than a state.

Four signals, and silence unless one fires:

  narration    added lines that only parse for someone who saw the diff.
  repetition   something the file already said, which is staleness in the act.
  appending    growth that replaced nothing, which is how a doc goes stale.
  size         a file already past what anyone reads to the end of.

The events are the ones comment-guard watches, for the same reasons: the
snapshot at SessionStart makes existing uncommitted work old news, PreToolUse
keeps the text a Write is about to destroy, and the Bash pass catches a
heredoc or a `sed -i` against the working tree.

Nothing is exempt for being a history. A changelog is the one file where
narrating belongs, which is exactly why an edit appending to one is worth
seeing: either it is a deliberate release note, or a change went somewhere it
did not belong. Only trees nobody here authors are skipped.

Silent on anything it cannot read, and the entry point is wrapped so a failure
can never break an edit.
"""

import difflib
import json
import os
import re
import sys

import guard_state
from guard_state import git, prune_old_state, read_text, save_state, toplevel

STATE_VERSION = 1
MAX_UNTRACKED_FILES = 500
MAX_PREWRITE_BYTES = 200_000
MAX_PREWRITE_KEPT = 8
MAX_FLAGGED = 200
MAX_LISTED = 12

# Measuring a document has its own ceiling, well above the one for reading a
# file's prose. A document large enough to refuse to read is the one the size
# signal most wants to raise, so the ceiling that decides how long it is has to
# sit above any length worth reporting.
MAX_DOC_BYTES = 4_000_000

# Growth that replaced nothing. Below this an edit is too small to be a section,
# and a fifth as much removed as added means the file was being rewritten.
GROWTH_LINES = 12
REWRITE_SHARE = 5

# Past this a file is no longer held in one head, so anything added to it is
# added where it will not be found.
BLOAT_LINES = 400
BLOAT_HEADINGS = 30

DOC_EXTENSIONS = (".md", ".markdown", ".mdown", ".mkd", ".mdx")

# A changelog is not exempt. Narrating changes is what one is for, so an edit
# that appends to it is either the rare deliberate release note or a sign that
# a change went somewhere it did not belong, and both are worth seeing. Only
# trees nobody here authors are skipped.
FOREIGN_DIRS = ("node_modules", "vendor")

FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
SETEXT = re.compile(r"^\s{0,3}(={2,}|-{2,})\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
FRONT = "---"

# A passage counts as one the document already carries when this much of its
# vocabulary is shared. Measured over 367 real documents, nothing reaches 0.6
# by accident; the four pairs that reach 0.5 are boilerplate.
REPEAT_LINES = 3
REPEAT_WORDS = 8
REPEAT_SHARE = 0.6
MAX_BLOCKS = 400

WORD = re.compile(r"[a-z0-9_.-]{3,}")
COMMON = set(
    "the and for that with this from are was were you your not but has have had"
    " its it's into out can will when which what where they them then than".split()
)

# "Now" carries most of it, and it carries it in front of anything at all --
# "now wired", "now the default", "now 76% of RTO". Only the readings that point
# at the clock rather than at a previous version are held out.
INSTRUCTION = (
    "run open edit add install restart create copy paste check visit click set"
    " save commit push deploy start stop try use read make"
).replace(" ", " | ")

NOW = rf"""(?<!right\ )(?<!just\ )(?<!\bby\ )(?<!for\ )(?<!until\ )(?<!\bup\ to\ )
          (?<!as\ of\ )(?<!even\ )(?<!\band\ )
          now (?! (?:\s*,\s*then)
                | \s+ (?: that | what | and\ then | on\b | is\ the
                        | (?: {INSTRUCTION} )\b ) )"""

# A change stated as a move between two values, which is the other shape it
# takes: "renamed as", "changed from true to false", "bumped to 0.1.10".
MOVED = r"""(?: renamed | superseded | retired | backported | obsoleted )
          | (?: changed | moved | bumped | dropped | lowered | raised | switched
              | migrated | reduced | increased | promoted | rewritten | reworked )
            \s+ (?: from \s+ \S+ \s+ )? to \b"""

# Prose that only means something to a reader who saw the previous version.
NARRATION = re.compile(
    rf"""
      ^\#{{1,6}}\s*(?: change\s?log | changes | recent\ changes | recent\ updates
                     | what's\ new | whats\ new | revision\ history
                     | release\ notes | migration\ notes | updates )\s*:?\s*$
    | ^(?:[-*+]|\d+[.)])\s+\*{{0,2}}\d{{4}}-\d{{2}}-\d{{2}}
    | \b{NOW}\b
    | \bno\ longer\s+(?: uses | returns | supports | handles | reads | writes
                       | runs | exists | appears | applies )\b
    | \b(?: {MOVED} )
    | \b(?: previously | formerly | used\ to\b
          | as\ of\ this\ (?: change | commit | version | release )
          | in\ this\ (?: change | version | update | release )
          | this\ (?: change | commit | PR | patch )\ (?: adds | changes | removes
                                                        | updates | introduces ) )\b
    | \b(?: has | have | had | had\ not )\ been\ (?: updated | changed | renamed
          | moved | replaced | removed | added | introduced | rewritten )\b
    | \bwas\ (?: renamed | removed | replaced | moved | introduced | split
               | merged | dropped | deleted )\b
    | \bwe\ (?: added | changed | removed | updated | renamed | moved
              | introduced | replaced )\b
    | \b(?: added | updated | changed | removed | fixed | introduced )\s+
      (?: support\ for | handling\ of )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

RULE = """A document describes what is true now, to a reader who never saw the
version before it. Put the edit through all four:

  Delete a sentence and ask what a reader loses. If the answer is what changed,
  it belongs in the commit message. "Now", "previously" and "no longer" have no
  referent for someone arriving today.

  Before adding a section, find the one it should have replaced. A document
  that tracks its subject is rewritten about as often as it grows; one that is
  only ever appended to is stale in two places at once.

  Anything that must stay current is safer where it cannot drift -- a test, a
  --help output, a generated table. Prose about behaviour drifts in silence.

  A file too long to hold in one head is not read to the end. If a section has
  outlived its purpose, cut it rather than write around it.

Rewrite or cut what does not survive. Do not report back on what you cut."""


def is_doc(path):
    """Whether this path is a document the rule applies to."""
    parts = path.replace("\\", "/").lower().split("/")
    if any(part in FOREIGN_DIRS for part in parts[:-1]):
        return False
    return os.path.splitext(parts[-1])[1] in DOC_EXTENSIONS


def body_of(lines):
    """A whole document less its front matter, which answers to a tool."""
    if not lines or lines[0].strip() != FRONT:
        return lines
    for index in range(1, min(len(lines), 50)):
        if lines[index].strip() in (FRONT, "..."):
            return lines[index + 1 :]
    return lines


def prose(lines):
    """The lines a reader reads: no fenced code, no indented code."""
    kept = []
    fence = None
    for line in lines:
        opener = FENCE.match(line)
        if fence:
            if opener and line.strip().startswith(fence):
                fence = None
            continue
        if opener:
            fence = opener.group(1)[:3]
            continue
        if line[:4] == "    " or line[:1] == "\t":
            if not LIST_ITEM.match(line):
                continue
        kept.append(line)
    return kept


def weight(lines):
    """How many of these lines a reader would count as content."""
    return sum(1 for line in lines if line.strip())


def narrating(lines):
    """The added lines that describe the change rather than the thing."""
    found = []
    for line in prose(lines):
        text = line.strip()
        if text and NARRATION.search(text):
            found.append(text)
    return list(dict.fromkeys(found))


def headed(lines):
    """Whether each line opens a section, counting both ways of writing one."""
    for index, line in enumerate(lines):
        if HEADING.match(line):
            yield index, True
        elif (
            SETEXT.match(line)
            and index
            and lines[index - 1].strip()
            and not HEADING.match(lines[index - 1])
        ):
            yield index, True
        else:
            yield index, False


def heading_texts(lines):
    """Every section title in these lines, as a reader would say it aloud."""
    titles = []
    for index, is_heading in headed(lines):
        if not is_heading:
            continue
        match = HEADING.match(lines[index])
        title = match.group(2) if match else lines[index - 1]
        titles.append(" ".join(title.lower().split()).strip("#* "))
    return [t for t in titles if t]


def measure(text):
    """(content lines, headings) of a whole document."""
    lines = prose(body_of(text.splitlines()))
    return weight(lines), sum(1 for _, is_heading in headed(lines) if is_heading)


def measure_file(path):
    """(content lines, headings) of the document on disk, or (0, 0) unread."""
    try:
        if os.path.getsize(path) > MAX_DOC_BYTES:
            return 0, 0
        with open(path, encoding="utf-8", errors="replace") as handle:
            return measure(handle.read())
    except Exception:
        return 0, 0


def passages(lines):
    """The document in the units a reader takes it in: blank-line-separated."""
    found, current = [], []
    for line in lines:
        if line.strip():
            current.append(line.strip())
        elif current:
            found.append(current)
            current = []
    if current:
        found.append(current)
    return found


def vocabulary(passage):
    return {w for w in WORD.findall(" ".join(passage).lower()) if w not in COMMON}


def tabular(passage):
    """A table repeats its header wherever it appears, and means it every time."""
    return sum(1 for line in passage if line.startswith("|")) * 2 > len(passage)


def shared(one, other):
    return len(one & other) / len(one | other) if one | other else 0.0


def repeats(added, text):
    """What this edit added that the document already said somewhere else.

    The section that should have been replaced is still there, one screen up,
    and now two parts of the file claim to be true. That is what going stale
    looks like from the outside, and it is visible without knowing the subject:
    a heading that now appears twice, or a passage whose words the file already
    carries.

    The added passage is itself part of the document by the time this runs, so
    a passage matching only once has matched itself.
    """
    document = prose(body_of(text.splitlines()))
    found = []

    titles = heading_texts(document)
    for title in dict.fromkeys(heading_texts(prose(added))):
        if titles.count(title) > 1:
            found.append(f'the heading "{title}" now appears {titles.count(title)} times')

    blocks = passages(document)
    if len(blocks) > MAX_BLOCKS:
        return found
    known = [vocabulary(block) for block in blocks]
    for block in passages(prose(added)):
        if len(block) < REPEAT_LINES or tabular(block):
            continue
        mine = vocabulary(block)
        if len(mine) < REPEAT_WORDS:
            continue
        if sum(1 for other in known if shared(mine, other) >= REPEAT_SHARE) > 1:
            found.append(f'the passage beginning "{block[0][:60]}" is already in the file')
    return found


def delta(before, after):
    """(added lines, removed count) between two versions of a snippet."""
    old, new = before.splitlines(), after.splitlines()
    added, removed = [], 0
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new[j1:j2])
        if tag in ("delete", "replace"):
            removed += weight(old[i1:i2])
    return added, removed


def judge(added, removed, narration, repetition, size, headings, new):
    """Why this edit is worth raising, as a list of reasons. Empty when it is not.

    A file being born is exempt from growth and from length. It replaced nothing
    because there was nothing to replace, and its length was chosen whole rather
    than accumulated. It can still narrate, and it can still say a thing twice.
    """
    reasons = []
    if narration:
        reasons.append("narration")
    if repetition:
        reasons.append("repetition")
    if new or added <= removed:
        return reasons
    if added - removed >= GROWTH_LINES and removed * REWRITE_SHARE < added:
        reasons.append("appending")
    if size >= BLOAT_LINES or headings >= BLOAT_HEADINGS:
        reasons.append("size")
    return reasons


def describe(label, name, added, removed, narration, repetition, size, headings, reasons):
    """What one document's report says, as a paragraph."""
    opening = f"{label} {name}: {added} line(s) added, {removed} replaced."
    if size:
        opening += f" The file is now {size} lines under {headings} heading(s)."
    parts = [opening]
    if "appending" in reasons:
        parts.append(
            "It grew without replacing anything, which is how a document goes stale."
        )
    if "size" in reasons:
        parts.append("It is already past the length anyone reads to the end of.")
    if repetition:
        listing = "\n".join(f"  {line}" for line in repetition[:MAX_LISTED])
        parts.append(
            f"The file now says {len(repetition)} thing(s) twice, which is the "
            f"section that should have been replaced still sitting there:\n\n{listing}"
        )
    if narration:
        listing = "\n".join(f"  {line}" for line in narration[:MAX_LISTED])
        parts.append(
            f"{len(narration)} added line(s) describe the change rather than the "
            f"thing:\n\n{listing}"
        )
    return "\n\n".join(parts)


def announce(bodies):
    """One event says one thing, however many documents it took.

    A hook's stdout is read as a single object, so a report per document is a
    report that cannot be read at all.
    """
    if not bodies:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n\n".join(bodies) + f"\n\n{RULE}",
                },
                "suppressOutput": True,
            }
        )
    )


def worktree_docs(top):
    """Per document, what the working tree holds beyond HEAD: (added, removed, new)."""
    changes = {}
    current = None
    born = False
    for line in git(top, "diff", "HEAD", "--unified=0", "--no-color").splitlines():
        if line.startswith("diff --git "):
            current, born = None, False
        elif line.startswith("--- "):
            born = line[4:].strip() == "/dev/null"
        elif line.startswith("+++ "):
            target = line[4:].strip()
            name = "" if target == "/dev/null" else target[2:]
            current = name if name and is_doc(name) else None
            if current:
                changes[current] = [[], 0, born]
        elif current and line.startswith("+"):
            changes[current][0].append(line[1:])
        elif current and line.startswith("-"):
            changes[current][1] += 1 if line[1:].strip() else 0

    untracked = git(top, "ls-files", "--others", "--exclude-standard").splitlines()
    for name in untracked[:MAX_UNTRACKED_FILES]:
        if is_doc(name):
            body = read_text(os.path.join(top, name))
            changes[name] = [body.splitlines(), 0, True]
    return changes


def account_for(state, top):
    return state["repos"].setdefault(top, {})


def resync(state, top):
    """Zero a repository's counts when HEAD has moved out from under them.

    They say what the tree holds beyond HEAD, so a commit settles all of it at
    once. Without this the last figure stands as a mark nothing clears, and
    every append after the first commit of a session is silently swallowed.
    """
    head = git(top, "rev-parse", "HEAD").strip()
    if state["heads"].get(top) == head:
        return
    state["heads"][top] = head
    for known in account_for(state, top).values():
        known["added"] = known["removed"] = 0


def counted(account, name):
    entry = account.get(name) or {}
    return entry.get("added", 0), entry.get("removed", 0), entry.get("flagged", [])


def record(account, name, added, removed, flagged):
    """What of this document's growth has been raised already.

    The counts are what the tree holds beyond HEAD, so they follow HEAD when it
    moves: a commit takes the growth with it and the file starts from zero. The
    narration lines are a union, so a line deleted and written again is raised
    once.
    """
    known = account.setdefault(name, {"added": 0, "removed": 0, "flagged": []})
    known["added"] = added
    known["removed"] = removed
    merged = list(dict.fromkeys(known["flagged"] + list(flagged)))
    known["flagged"] = merged[-MAX_FLAGGED:]


def snapshot(state, top):
    """Take a repository's uncommitted documents as the baseline, once."""
    if not top or top in state["repos"]:
        return False
    state["heads"][top] = git(top, "rev-parse", "HEAD").strip()
    account = account_for(state, top)
    for name, (added, removed, _) in worktree_docs(top).items():
        record(account, name, weight(added), removed, narrating(added))
    return True


def named(path):
    """(work tree, the name to call the file by) -- the path a reader can act on.

    Documents share basenames across a tree the way code does not: a repository
    with `docs/api/auth.md` and `docs/web/auth.md` has two files that a report
    saying "auth.md" cannot tell apart.
    """
    top = toplevel(path)
    if not top:
        return "", os.path.basename(path)
    return top, os.path.relpath(path, top).replace("\\", "/")


def fold(state, top, name, flagged):
    """Credit an edit to its repository, so the Bash pass does not raise it again.

    What the edit added is already on disk by now, so the account takes the
    tree's own figures rather than the payload's. The two count differently --
    a payload says what this edit did, the tree says what HEAD is owed -- and
    only the second is what the Bash pass will compare against.
    """
    if not top:
        return
    fresh = snapshot(state, top)
    resync(state, top)
    account = account_for(state, top)
    if fresh:
        added, removed, _ = counted(account, name)
    else:
        current = worktree_docs(top).get(name)
        added, removed = (weight(current[0]), current[1]) if current else (0, 0)
    record(account, name, added, removed, flagged)


def on_session_start(payload, state, store):
    if snapshot(state, toplevel(payload.get("cwd") or os.getcwd())):
        save_state(store, state)


def on_pre_write(payload, state, store):
    """Keep what the file says now, before Write replaces it."""
    if payload.get("tool_name") != "Write":
        return
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not is_doc(path) or not os.path.exists(path):
        return
    body = read_text(path)
    if len(body) <= MAX_PREWRITE_BYTES:
        kept = state["prewrite"]
        kept[path] = body
        # A Write that never lands -- one the user declines -- leaves its copy
        # behind, so only the last few are carried.
        for stale in list(kept)[:-MAX_PREWRITE_KEPT]:
            del kept[stale]
        save_state(store, state)


def committed_version(top, name):
    return git(top, "show", f"HEAD:{name}") if top else ""


def on_edit(payload, state, store):
    tool = payload.get("tool_name") or ""
    args = payload.get("tool_input") or {}
    path = args.get("file_path") or ""
    if not is_doc(path):
        return
    top, name = named(path)

    if tool == "Edit":
        before, after = args.get("old_string") or "", args.get("new_string") or ""
        new = False
    elif tool == "MultiEdit":
        edits = args.get("edits") or []
        before = "\n".join(e.get("old_string") or "" for e in edits)
        after = "\n".join(e.get("new_string") or "" for e in edits)
        new = False
    elif tool == "Write":
        kept = state["prewrite"].pop(path, None)
        before = kept if kept is not None else committed_version(top, name)
        after = args.get("content") or ""
        new = not before.strip()
    else:
        return

    added, removed = delta(before, after)
    narration = narrating(added)
    repetition = repeats(added, read_text(path))
    size, headings = measure_file(path)
    fold(state, top, name, narration)
    save_state(store, state)

    reasons = judge(weight(added), removed, narration, repetition, size, headings, new)
    if reasons:
        announce(
            [
                describe(
                    "This edit to", name, weight(added), removed, narration,
                    repetition, size, headings, reasons,
                )
            ]
        )


def on_bash(payload, state, store):
    """Every repository in scope against its baseline, since a command says
    nothing about where it wrote."""
    if snapshot(state, toplevel(payload.get("cwd") or os.getcwd())):
        save_state(store, state)
        return

    raised = []
    for top in list(state["repos"]):
        resync(state, top)
        account = account_for(state, top)
        changes = worktree_docs(top)
        for name, known in account.items():
            if name not in changes:
                known["added"] = known["removed"] = 0
        for name, (added_lines, removed, new) in changes.items():
            added, flagged = weight(added_lines), narrating(added_lines)
            seen_added, seen_removed, seen_flagged = counted(account, name)
            record(account, name, added, removed, flagged)
            fresh = [line for line in flagged if line not in seen_flagged]
            grown, replaced = added - seen_added, max(removed - seen_removed, 0)
            path = os.path.join(top, name)
            size, headings = measure_file(path)
            doubled = repeats(added_lines, read_text(path)) if grown > 0 else []
            reasons = judge(
                grown, replaced, fresh, doubled, size, headings, new and not seen_added
            )
            if reasons:
                raised.append(
                    (top, name, grown, replaced, fresh, doubled, size, headings, reasons)
                )
    save_state(store, state)

    # A name is relative to its own work tree, so two trees in scope -- a
    # checkout and a worktree of it -- both hold a README.md and both would be
    # called one. Where that can happen, say which.
    several = len({top for top, *_ in raised}) > 1
    bodies = [
        describe(
            "A shell command changed",
            os.path.join(top, name) if several else name,
            *rest,
        )
        for top, name, *rest in raised[:MAX_LISTED]
    ]
    if len(raised) > MAX_LISTED:
        bodies.append(f"{len(raised) - MAX_LISTED} further document(s) changed the same way.")
    announce(bodies)


def load_state(path):
    stored = guard_state.load_state(path, STATE_VERSION) or {}
    return {
        "version": STATE_VERSION,
        "repos": stored.get("repos") or {},
        "heads": stored.get("heads") or {},
        "prewrite": stored.get("prewrite") or {},
    }


def main():
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name") or "PostToolUse"
    store = guard_state.state_file(payload.get("session_id"), "docs")
    prune_old_state()
    state = load_state(store)

    if event == "SessionStart":
        on_session_start(payload, state, store)
    elif event == "PreToolUse":
        on_pre_write(payload, state, store)
    elif payload.get("tool_name") == "Bash":
        on_bash(payload, state, store)
    else:
        on_edit(payload, state, store)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
