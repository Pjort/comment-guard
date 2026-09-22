#!/usr/bin/env python3
"""Surface comment lines as they land, whatever wrote them.

Comments get written to justify a diff to whoever is reading the conversation,
and that reader is gone by the time the code is read. Listing them back at the
moment they land is the only point where cutting one is still cheap.

Four events, because an edit is not the only way a file changes:

  SessionStart          snapshot the working tree, silently. Everything already
                        uncommitted has been decided on; only what lands after
                        this point is news.
  PreToolUse  Write     keep the comments the file had before it is overwritten.
                        By PostToolUse the old content is gone from disk.
  PostToolUse Edit      the tool payload gives the exact delta.
              Write
              MultiEdit
  PostToolUse Bash      the working tree against the snapshot, which also catches
                        heredocs, sed -i, cp, tee, and git checkout/merge/reset.

The snapshot is per repository and per session, so a line is raised once and
never again. An edit folds what it reported into the snapshot, so the next
shell command does not raise it a second time. A repository enters scope at
SessionStart or the first time an edit reaches it, which is what covers a
session begun a level up from the checkout, or one working across two.

A docstring counts. It is prose written to a reader, it drifts from the code
the same way, and the rule below applies to it whole.

Silent on anything it cannot parse. A hook that breaks an edit is worse than
one that misses a line, and a false positive costs a reader more than a miss,
so every doubtful case resolves to saying nothing.
"""

import functools
import json
import os
import re
import sys
from typing import NamedTuple

import guard_state
from guard_state import git, prune_old_state, read_text, save_state, toplevel

STATE_VERSION = 2
MAX_UNTRACKED_FILES = 500


class Syntax(NamedTuple):
    """How one language spells a comment, and what it opens a string with."""

    line: tuple = ()
    blocks: tuple = ()
    quotes: str = "\"'"
    docstring: bool = False


C_BLOCK = (("/*", "*/"),)
DOUBLE_ONLY = '"'

HASH = Syntax(line=("#",))
SLASH = Syntax(line=("//",), blocks=C_BLOCK)
RUST = Syntax(line=("//",), blocks=C_BLOCK, quotes=DOUBLE_ONLY)
LUA = Syntax(line=("--",), blocks=(("--[[", "]]"),))
SQL = Syntax(line=("--",), blocks=C_BLOCK)
HCL = Syntax(line=("#", "//"), blocks=C_BLOCK)
PYTHON = Syntax(line=("#",), docstring=True)
MARKUP = Syntax(blocks=(("<!--", "-->"),), quotes="")
COMPONENT = Syntax(line=("//",), blocks=(("<!--", "-->"), ("/*", "*/")))
LISP = Syntax(line=(";",), quotes=DOUBLE_ONLY)
BATCH = Syntax(line=("rem", "::"), quotes=DOUBLE_ONLY)
RUBY = Syntax(line=("#",), blocks=(("=begin", "=end"),))
ELIXIR = Syntax(line=("#",), docstring=True)
CSS = Syntax(blocks=C_BLOCK)
POWERSHELL = Syntax(line=("#",), blocks=(("<#", "#>"),))
HASKELL = Syntax(line=("--",), blocks=(("{-", "-}"),), quotes=DOUBLE_ONLY)
JULIA = Syntax(line=("#",), blocks=(("#=", "=#"),), docstring=True)
BASIC = Syntax(line=("'",), quotes=DOUBLE_ONLY)
GRAPHQL = Syntax(line=("#",), docstring=True)
OCAML = Syntax(blocks=(("(*", "*)"),), quotes=DOUBLE_ONLY)
FSHARP = Syntax(line=("//",), blocks=(("(*", "*)"),), quotes=DOUBLE_ONLY)
ERLANG = Syntax(line=("%",), quotes=DOUBLE_ONLY)
TEX = Syntax(line=("%",), quotes="")
FORTRAN = Syntax(line=("!",), quotes=DOUBLE_ONLY)
NIX = Syntax(line=("#",), blocks=C_BLOCK)
INI = Syntax(line=("#", ";"), quotes="")
PHP = Syntax(line=("//", "#"), blocks=C_BLOCK)
OBJC = SLASH


def table(rows):
    return {name: syntax for syntax, names in rows for name in names.split()}


EXTENSIONS = table(
    [
        (PYTHON, ".py .pyi .pyw"),
        (HASH, ".sh .bash .zsh .ksh .fish .pl .pm .yaml .yml .toml .r .cr .nim .awk"
               " .cmake .mk .mak .properties .tmpl"),
        (RUBY, ".rb .rake .gemspec .podspec .ru"),
        (HCL, ".tf .tfvars .hcl .nomad"),
        (SLASH, ".go .js .jsx .mjs .cjs .ts .tsx .mts .cts .java .c .h .cc .cpp .cxx"
                " .hh .hpp .hxx .cs .swift .kt .kts .scala .sbt"
                " .dart .groovy .gradle .zig .sol .proto .sv .svh .d .bicep .jsonc"
                " .json5 .scss .less .sass .styl"),
        (RUST, ".rs"),
        (OBJC, ".m .mm"),
        (PHP, ".php .phtml"),
        (SQL, ".sql .psql .pgsql .plsql"),
        (LUA, ".lua"),
        (MARKUP, ".html .htm .xhtml .xml .svg .xsl .xslt .plist .xaml .storyboard"
                 " .csproj .fsproj .vbproj .props .targets .resx .wxs"),
        (COMPONENT, ".vue .svelte"),
        (LISP, ".clj .cljs .cljc .edn .el .lisp .lsp .scm .ss .rkt"),
        (BATCH, ".bat .cmd"),
        (ELIXIR, ".ex .exs"),
        (CSS, ".css"),
        (POWERSHELL, ".ps1 .psm1 .psd1"),
        (HASKELL, ".hs .lhs"),
        (JULIA, ".jl"),
        (BASIC, ".vb .vbs .bas"),
        (GRAPHQL, ".graphql .gql"),
        (OCAML, ".ml .mli"),
        (FSHARP, ".fs .fsx .fsi"),
        (ERLANG, ".erl .hrl"),
        (TEX, ".tex .sty .cls .bib"),
        (FORTRAN, ".f90 .f95 .f03 .f08"),
        (NIX, ".nix"),
        (INI, ".ini .cfg .conf .service .desktop"),
    ]
)

BASENAMES = table(
    [
        (HASH, "dockerfile containerfile makefile gnumakefile justfile cmakelists.txt"
               " .env .gitignore .dockerignore .gitattributes .editorconfig .npmrc"
               " .bashrc .zshrc .bash_profile .profile .gitconfig"),
        (RUBY, "rakefile gemfile vagrantfile brewfile podfile fastfile appfile"),
    ]
)

# Machine-read directives answer to a tool, not to a reader, so the rule below
# does not apply to them.
DIRECTIVE = re.compile(
    r"""^(?: \#!
          | \#\s*(?: -\*- | coding[:=] | type:\s*ignore | fmt: | isort:
                   | (?: noqa | pragma | pylint | mypy | ruff | nosec
                       | shellcheck | yamllint | codespell ) (?![\w-]) )
          | //\s*(?: go: | @ts- | lint: | coverage:
                   | (?: nolint | eslint | prettier | tslint ) (?![\w-]) )
          | --\s*noqa\b )""",
    re.IGNORECASE | re.VERBOSE,
)

# A block comment's inner line, as a hunk starting mid-block shows it. The
# space after the star is what keeps C's `*ptr = x;` out.
BLOCK_CONT = re.compile(r"^\*(\s|$)")

DOC_OPEN = re.compile(
    r"""^\s*(?:@(?:module|type)?doc\s+)?[rRuUbBfF]{0,2}(?P<quote>\"\"\"|''')"""
)

RULE = """Put each through all four before moving on:

  Delete it and ask what a reader loses. If the answer is why the change was
  made, it belongs in the commit message, not the code.

  A comment is not justified by its category. "It's a regex", "it's public API",
  "it's external knowledge" describe the kind, not the line -- check whether a
  name, a type, or a test name already carries it.

  Behaviour that needs explaining needs a named test, not a comment. A test
  can't drift. Rename the test rather than keep the comment.

  Delete a comment you wrote while making the change. Written then, it explains
  the diff rather than the code, and the diff is what the commit message is for.

Cut what does not survive. Do not report back on the ones you cut."""


def syntax_for(path):
    """The language of a file, by name."""
    base = os.path.basename(path).lower()
    if base in BASENAMES:
        return BASENAMES[base]
    extension = os.path.splitext(base)[1]
    if extension in EXTENSIONS:
        return EXTENSIONS[extension]
    return BASENAMES.get(base.rsplit(".", 1)[0])


def strip_block(line, block):
    """One line of a block comment, less its delimiters and any JSDoc star."""
    text = line.strip()
    if text.startswith(block[0]):
        text = text[len(block[0]) :]
    if text.endswith(block[1]):
        text = text[: -len(block[1])]
    return text.strip().lstrip("*").strip()


def block_text(line, block):
    """What a block-comment line says, or None when it carries nothing to read.

    The delimiter lines of a JSDoc block hold no prose, and a directive in
    block form answers to a tool the same as one on a `//` line.
    """
    text = strip_block(line, block)
    return text if text and not DIRECTIVE.match(f"// {text}") else None


def find_comment(line, syntax):
    """The comment part of one line, or None. Skips markers inside string literals."""
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in syntax.quotes:
            quote = char
        elif char == "\\":
            index += 1
        elif opens_at(line, index, syntax):
            return line[index:].strip()
        index += 1
    return None


@functools.lru_cache(maxsize=None)
def markers(syntax):
    """Everything that opens a comment, punctuation first, words second."""
    every = syntax.line + tuple(block[0] for block in syntax.blocks)
    return (
        tuple(m for m in every if not m[0].isalpha()),
        tuple(m for m in every if m[0].isalpha()),
    )


def opens_at(line, index, syntax):
    """Whether a comment starts here, a word marker standing as a word."""
    if index and line[index - 1] == ":":
        return False
    punctuation, words = markers(syntax)
    if line.startswith(punctuation, index):
        return True
    for marker in words:
        if line[index : index + len(marker)].lower() == marker:
            before = line[index - 1] if index else " "
            after = line[index + len(marker) : index + len(marker) + 1] or " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return True
    return False


def block_opened(comment, syntax):
    return next((block for block in syntax.blocks if comment.startswith(block[0])), None)


def open_docstring(line, syntax):
    """(delimiter, what follows it) when this line opens a docstring, else None."""
    match = syntax.docstring and DOC_OPEN.match(line)
    return (match.group("quote"), line[match.end() :]) if match else None


def open_fence(line, syntax):
    """(delimiter, index) of a triple quote this line leaves open, else None."""
    index, state, start, quote = 0, None, 0, None
    while index < len(line):
        char = line[index]
        if state:
            if line.startswith(state, index):
                state = None
                index += 3
                continue
        elif quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif line.startswith(('"""', "'''"), index):
            state, start = line[index : index + 3], index
            index += 3
            continue
        elif char in syntax.quotes:
            quote = char
        elif opens_at(line, index, syntax):
            break
        index += 1
    return (state, start) if state else None


def consume(line, closer):
    """What a run holds up to `closer`, and what is left after it, or None."""
    at = line.find(closer)
    return (line, None) if at == -1 else (line[:at], line[at + len(closer) :])


def comment_lines(text, syntax):
    """Comments in a whole file or snippet, tracking multi-line state.

    Three kinds of it. Without the triple-quote fence a regex or SQL string that
    happens to hold a marker reads as a comment on every line it spans; without
    the `/* ... */` state a block comment is seen only on the line it opens on,
    which for a JSDoc block is the line that says nothing; and a docstring is
    prose to a reader only until its closing quote.
    """
    found = []
    fence = doc = inside = None
    for line in text.splitlines():
        if inside:
            body, line = consume(line, inside[1])
            keep = block_text(body, inside)
            if keep:
                found.append(keep)
            if line is None:
                continue
            inside = None
        if doc:
            body, line = consume(line, doc)
            if body.strip():
                found.append(body.strip())
            if line is None:
                continue
            doc = None
        if fence:
            _, line = consume(line, fence)
            if line is None:
                continue
            fence = None
        opened = open_docstring(line, syntax)
        if opened:
            doc, rest = opened
            body, line = consume(rest, doc)
            if body.strip():
                found.append(body.strip())
            if line is None:
                continue
            doc = None
        opened = open_fence(line, syntax)
        if opened:
            line = line[: opened[1]]
            fence = opened[0]
        comment = find_comment(line, syntax)
        if comment is None:
            continue
        block = block_opened(comment, syntax)
        if block:
            body, rest = consume(comment[len(block[0]) :], block[1])
            inside = block if rest is None else None
            keep = block_text(body, block)
            if keep:
                found.append(keep)
        elif not DIRECTIVE.match(comment):
            found.append(comment)
    return found


def diff_comment(line, syntax):
    """The comment one added line carries, or None.

    A hunk can begin inside a block comment, so a continuation line is judged
    on its own shape -- a lone star, or a closing delimiter -- rather than on
    an opener the diff never showed.
    """
    if open_docstring(line, syntax):
        return next(iter(comment_lines(line, syntax)), None)
    comment = find_comment(line, syntax)
    if comment is not None:
        block = block_opened(comment, syntax)
        if block:
            return block_text(comment, block)
        return None if DIRECTIVE.match(comment) else comment
    for block in syntax.blocks:
        if BLOCK_CONT.match(line.strip()) or line.rstrip().endswith(block[1]):
            return block_text(line, block)
    return None


def file_comments(path, text=None):
    syntax = syntax_for(path)
    if not syntax:
        return []
    body = read_text(path) if text is None else text
    return comment_lines(body, syntax)


def worktree_comments(top):
    """Every comment line the working tree has beyond HEAD, tracked or not."""
    found = []
    diff = git(top, "diff", "HEAD", "--unified=0", "--no-color")
    syntax = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            syntax = None if target == "/dev/null" else syntax_for(target[2:])
        elif line.startswith("+") and not line.startswith("+++") and syntax:
            comment = diff_comment(line[1:], syntax)
            if comment:
                found.append(comment)

    untracked = git(top, "ls-files", "--others", "--exclude-standard").splitlines()
    for name in untracked[:MAX_UNTRACKED_FILES]:
        if syntax_for(name):
            found.extend(file_comments(os.path.join(top, name)))
    return found


def committed_version(path):
    top = toplevel(path)
    if not top:
        return ""
    return git(top, "show", f"HEAD:{os.path.relpath(path, top)}")


def load_state(path):
    stored = guard_state.load_state(path, STATE_VERSION) or {}
    return {
        "version": STATE_VERSION,
        "repos": stored.get("repos") or {},
        "prewrite": stored.get("prewrite") or {},
    }


def snapshot(state, top):
    """Take a repository's current comments as the baseline, once."""
    if top and top not in state["repos"]:
        state["repos"][top] = sorted(set(worktree_comments(top)))
        return True
    return False


def fold(state, path, comments):
    """Credit an edit's comments to its repository, so the Bash path skips them.

    A repository first reached by an edit is snapshotted here rather than at
    SessionStart. That is what covers a session begun a level up from the
    checkout, or one that reaches sideways into a second one.
    """
    top = toplevel(path)
    if not top:
        return
    snapshot(state, top)
    state["repos"][top] = sorted(set(state["repos"][top]) | set(comments))


def report(label, added, removed=None):
    """The listing, with the tally only where a removal count was measured.

    The Bash path has none to give: it compares against a snapshot that only
    ever grows, which shows what arrived but not what left.
    """
    listing = "\n".join(f"  {line}" for line in added)
    tally = (
        ""
        if removed is None
        else f" -- {len(removed)} removed, net {len(added) - len(removed):+d}"
    )
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"{label} added {len(added)} comment line(s){tally}:\n\n"
                    f"{listing}\n\n{RULE}"
                ),
            }
        )
    )


def on_session_start(payload, state, store):
    if snapshot(state, toplevel(payload.get("cwd") or os.getcwd())):
        save_state(store, state)


def on_pre_write(payload, state, store):
    """Keep what the file says now, before Write replaces it."""
    if payload.get("tool_name") != "Write":
        return
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not syntax_for(path):
        return
    state["prewrite"][path] = sorted(set(file_comments(path)))
    save_state(store, state)


def on_bash(payload, state, store):
    """Every repository in scope against its snapshot, not just the session's.

    A command is free to write anywhere, and says nothing about where it did.
    Each repository the session has seen is cheaper to re-diff than to guess
    about.
    """
    if snapshot(state, toplevel(payload.get("cwd") or os.getcwd())):
        save_state(store, state)
        return
    added = []
    for top, known in list(state["repos"].items()):
        known = set(known)
        current = worktree_comments(top)
        added.extend(c for c in current if c not in known)
        state["repos"][top] = sorted(known | set(current))
    save_state(store, state)
    added = list(dict.fromkeys(added))
    if added:
        report("A shell command", added)


def on_edit(payload, state, store):
    tool = payload.get("tool_name") or ""
    args = payload.get("tool_input") or {}
    path = args.get("file_path") or ""
    syntax = syntax_for(path)
    if not syntax:
        return

    if tool == "Edit":
        known = set(comment_lines(args.get("old_string") or "", syntax))
        after = args.get("new_string") or ""
    elif tool == "MultiEdit":
        edits = args.get("edits") or []
        known = set(comment_lines("\n".join(e.get("old_string") or "" for e in edits), syntax))
        after = "\n".join(e.get("new_string") or "" for e in edits)
    elif tool == "Write":
        # PreToolUse read the file it replaced. Without that -- a session that
        # began mid-flight -- HEAD is the closest thing left to the old content.
        kept = state["prewrite"].pop(path, None)
        known = set(kept if kept is not None else comment_lines(committed_version(path), syntax))
        after = args.get("content") or ""
    else:
        return

    surviving = comment_lines(after, syntax)
    added = list(dict.fromkeys(c for c in surviving if c not in known))
    removed = sorted(known - set(surviving))
    fold(state, path, added)
    save_state(store, state)
    if added:
        report(f"This edit to {os.path.basename(path)}", added, removed)


def main():
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name") or "PostToolUse"
    store = guard_state.state_file(payload.get("session_id"), "comments")
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
