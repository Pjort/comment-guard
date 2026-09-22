#!/usr/bin/env python3
"""Run with: python3 -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins",
    "comment-guard",
    "hooks",
    "comment_guard.py",
)


class HookCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="comment-guard-repo-")
        self.state = tempfile.mkdtemp(prefix="comment-guard-state-")
        self.session = "test-session"
        self.git("init", "-q", ".")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "Test")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def git(self, *args, repo=None):
        subprocess.run(
            ["git", "-C", repo or self.repo, *args], capture_output=True, text=True, check=False
        )

    def commit(self, message="wip", repo=None):
        self.git("add", "-A", repo=repo)
        self.git("commit", "-qm", message, repo=repo)

    def write(self, name, text, repo=None):
        path = os.path.join(repo or self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def fire(self, event, tool=None, tool_input=None, cwd=None):
        """Run the hook and return the comment lines it reported, as a list."""
        payload = {
            "session_id": self.session,
            "cwd": cwd or self.repo,
            "hook_event_name": event,
        }
        if tool:
            payload["tool_name"] = tool
            payload["tool_input"] = tool_input or {}
        done = subprocess.run(
            ["python3", HOOK],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**os.environ, "COMMENT_GUARD_STATE_DIR": self.state},
            timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        if not done.stdout.strip():
            return []
        context = json.loads(done.stdout)["reason"]
        listing = context.split("\n\n")[1]
        return [line.strip() for line in listing.splitlines()]

    def session_start(self, cwd=None):
        return self.fire("SessionStart", cwd=cwd)

    def edit(self, path, old, new, cwd=None):
        return self.fire(
            "PostToolUse",
            "Edit",
            {"file_path": path, "old_string": old, "new_string": new},
            cwd=cwd,
        )

    def write_tool(self, path, content, cwd=None):
        self.fire("PreToolUse", "Write", {"file_path": path, "content": content}, cwd=cwd)
        with open(path, "w") as handle:
            handle.write(content)
        return self.fire(
            "PostToolUse", "Write", {"file_path": path, "content": content}, cwd=cwd
        )

    def bash(self, command="true", cwd=None):
        return self.fire("PostToolUse", "Bash", {"command": command}, cwd=cwd)


class Blocking(HookCase):
    def emitted(self, tool, tool_input):
        payload = {
            "session_id": self.session,
            "cwd": self.repo,
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
        }
        done = subprocess.run(
            ["python3", HOOK],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**os.environ, "COMMENT_GUARD_STATE_DIR": self.state},
            timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else {}

    def raw(self, path, old, new):
        return self.emitted("Edit", {"file_path": path, "old_string": old, "new_string": new})

    def test_a_report_blocks_rather_than_only_advising(self):
        path = self.write("a.py", "def f():\n    return 1\n")
        self.commit()
        emitted = self.raw(path, "def f():", "# why this exists\ndef f():")

        self.assertEqual(emitted.get("decision"), "block")
        self.assertIn("# why this exists", emitted.get("reason", ""))

    def test_an_edit_that_adds_no_comment_does_not_block(self):
        path = self.write("a.py", "x = 1\n")
        self.commit()

        self.assertEqual(self.raw(path, "x = 1", "x = 2"), {})

    def test_rewording_a_comment_is_reported_as_a_net_of_zero(self):
        path = self.write("a.py", "# the long original wording\nx = 1\n")
        self.commit()
        emitted = self.raw(path, "# the long original wording", "# shorter wording")

        self.assertIn("1 removed, net +0", emitted.get("reason", ""))

    def test_cutting_more_than_is_added_reports_a_negative_net(self):
        path = self.write("a.py", "# one\n# two\n# three\nx = 1\n")
        self.commit()
        emitted = self.raw(path, "# one\n# two\n# three", "# just one now")

        self.assertIn("3 removed, net -2", emitted.get("reason", ""))

    def test_a_shell_command_makes_no_claim_about_what_it_removed(self):
        self.write("a.py", "# kept\n# doomed\nx = 1\n")
        self.commit()
        self.session_start()
        self.write("a.py", "# kept\n# brand new note\nx = 1\n")
        emitted = self.emitted("Bash", {"command": "sed -i ..."})

        self.assertIn("added 1 comment line(s):", emitted.get("reason", ""))
        self.assertNotIn("removed", emitted.get("reason", ""))


class Reporting(HookCase):
    def test_edit_adding_a_comment_reports_it(self):
        path = self.write("a.py", "def f():\n    return 1\n")
        self.commit()
        added = self.edit(path, "def f():", "# why this exists\ndef f():")
        self.assertEqual(added, ["# why this exists"])

    def test_tool_directive_is_not_a_comment(self):
        path = self.write("a.py", "x = 1\n")
        self.commit()
        self.assertEqual(self.edit(path, "x = 1", "x = 1  # noqa: E501"), [])

    def test_a_word_that_merely_begins_with_a_directive_is_still_prose(self):
        path = self.write("a.py", "x = 1\n")
        self.commit()
        added = self.edit(path, "x = 1", "# pragmatic split, found by experiment\nx = 1")
        self.assertEqual(added, ["# pragmatic split, found by experiment"])

    def test_marker_inside_a_string_literal_is_not_a_comment(self):
        path = self.write("a.py", "x = 1\n")
        self.commit()
        self.assertEqual(self.edit(path, "x = 1", 'p = re.compile(r"^#\\d+")'), [])

    def test_block_comment_reports_prose_not_delimiters(self):
        path = self.write("a.ts", "export const x = 1\n")
        self.commit()
        added = self.edit(
            path,
            "export const x = 1",
            "/**\n * Adds two numbers.\n */\nexport const x = 1",
        )
        self.assertEqual(added, ["Adds two numbers."])

    def test_apostrophe_in_a_comment_does_not_hide_the_next_one(self):
        path = os.path.join(self.repo, "q.py")
        self.session_start()
        added = self.write_tool(path, "a = 1  # don't do this\nb = 2  # second note\n")
        self.assertEqual(added, ["# don't do this", "# second note"])

    def test_file_type_without_known_markers_is_ignored(self):
        path = self.write("notes.txt", "hello\n")
        self.commit()
        self.assertEqual(self.edit(path, "hello", "# not a comment here\nhello"), [])

    def test_edit_outside_a_git_repo_still_reports(self):
        outside = tempfile.mkdtemp(prefix="comment-guard-bare-")
        try:
            path = os.path.join(outside, "a.py")
            added = self.fire(
                "PostToolUse",
                "Edit",
                {"file_path": path, "old_string": "x = 1", "new_string": "# a note\nx = 1"},
            )
            self.assertEqual(added, ["# a note"])
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class Docstrings(HookCase):
    def test_python_docstring_reports_its_prose(self):
        path = self.write("a.py", "def f():\n    return 1\n")
        self.commit()
        added = self.edit(
            path, "    return 1", '    """Return one."""\n    return 1'
        )
        self.assertEqual(added, ["Return one."])

    def test_a_triple_quote_inside_a_string_opens_nothing(self):
        path = self.write("a.py", "x = 1\n")
        self.commit()
        added = self.edit(
            path, "x = 1", "pattern = \"'''\"\n# still a comment\ny = 2"
        )
        self.assertEqual(added, ["# still a comment"])

    def test_a_string_with_a_name_in_front_of_it_is_data(self):
        path = self.write("a.py", "x = 1\n")
        self.commit()
        self.assertEqual(
            self.edit(path, "x = 1", 'SQL = """\nselect 1\n"""'), []
        )


class Languages(HookCase):
    def test_html_comment_reports_its_prose(self):
        path = self.write("page.html", "<p>hi</p>\n")
        self.commit()
        added = self.edit(path, "<p>hi</p>", "<!-- why this markup -->\n<p>hi</p>")
        self.assertEqual(added, ["why this markup"])

    def test_apostrophe_in_markup_text_does_not_hide_a_comment(self):
        path = self.write("page.html", "<p>hi</p>\n")
        self.commit()
        added = self.edit(path, "<p>hi</p>", "<p>don't</p> <!-- a note -->")
        self.assertEqual(added, ["a note"])

    def test_rust_lifetime_does_not_hide_the_comment_after_it(self):
        path = self.write("a.rs", "fn f() {}\n")
        self.commit()
        added = self.edit(path, "fn f() {}", "fn f<'a>(x: &str) {} // a note")
        self.assertEqual(added, ["// a note"])

    def test_lua_block_comment_reports_its_prose(self):
        path = self.write("a.lua", "local x = 1\n")
        self.commit()
        added = self.edit(
            path, "local x = 1", "--[[\nwhy this exists\n]]\nlocal x = 1"
        )
        self.assertEqual(added, ["why this exists"])

    def test_vue_reports_both_its_template_and_its_script_comments(self):
        path = self.write("a.vue", "<template></template>\n")
        self.commit()
        added = self.edit(
            path,
            "<template></template>",
            "<template>\n"
            "  <!-- markup note -->\n"
            "</template>\n"
            "<script>\n"
            "/* script note */\n"
            "// line note\n"
            "</script>",
        )
        self.assertEqual(added, ["markup note", "script note", "// line note"])

    def test_a_quoted_list_does_not_hide_the_comment_after_it(self):
        path = self.write("a.clj", "(def xs [])\n")
        self.commit()
        added = self.edit(path, "(def xs [])", "(def xs '(1 2)) ; a note")
        self.assertEqual(added, ["; a note"])

    def test_a_word_marker_needs_a_word_boundary(self):
        path = self.write("a.bat", "echo hi\n")
        self.commit()
        added = self.edit(path, "echo hi", "set REMOTE=1\nREM a note")
        self.assertEqual(added, ["REM a note"])

    def test_a_percent_in_a_dot_m_file_is_modulo_not_a_comment(self):
        path = self.write("a.m", "int x = 1;\n")
        self.commit()
        added = self.edit(path, "int x = 1;", "if (i % 2) { } // objc note")
        self.assertEqual(added, ["// objc note"])

    def test_php_spells_a_line_comment_two_ways(self):
        path = self.write("a.php", "<?php\n$x = 1;\n")
        self.commit()
        added = self.edit(path, "$x = 1;", "# php hash note\n$x = 1;")
        self.assertEqual(added, ["# php hash note"])

    def test_a_url_scheme_is_not_a_comment(self):
        path = self.write("a.scss", ".a { color: red }\n")
        self.commit()
        added = self.edit(
            path, ".a { color: red }", ".a { background: url(http://x/i.png) }"
        )
        self.assertEqual(added, [])

    def test_an_escaped_marker_is_not_a_comment(self):
        path = self.write("a.tex", "\\section{A}\n")
        self.commit()
        added = self.edit(path, "\\section{A}", "50\\% done\n\\section{A}")
        self.assertEqual(added, [])

    def test_ruby_block_comment_reports_its_prose(self):
        path = self.write("a.rb", "x = 1\n")
        self.commit()
        added = self.edit(path, "x = 1", "=begin\nwhy this exists\n=end\nx = 1")
        self.assertEqual(added, ["why this exists"])

    def test_elixir_doc_attribute_reports_its_prose(self):
        path = self.write("a.ex", "def f, do: 1\n")
        self.commit()
        added = self.edit(
            path, "def f, do: 1", '@doc """\nReturns one.\n"""\ndef f, do: 1'
        )
        self.assertEqual(added, ["Returns one."])

    def test_a_suffixed_dockerfile_is_still_a_dockerfile(self):
        path = self.write("Dockerfile.dev", "FROM alpine\n")
        self.commit()
        added = self.edit(path, "FROM alpine", "# why alpine\nFROM alpine")
        self.assertEqual(added, ["# why alpine"])

    def test_an_extension_outranks_a_basename_it_happens_to_start_with(self):
        path = self.write("makefile.py", "x = 1\n")
        self.commit()
        added = self.edit(path, "x = 1", '"""Not a shell comment."""\nx = 1')
        self.assertEqual(added, ["Not a shell comment."])


SAMPLES = {
    "a.css": ("/* css note */\nbody { color: red }\n", "css note"),
    "a.scss": ("// scss note\n$x: 1;\n", "// scss note"),
    "a.ps1": ("<#\npowershell note\n#>\n$x = 1\n", "powershell note"),
    "a.hs": ("{- haskell note -}\nmain = return ()\n", "haskell note"),
    "a.jl": ("#= julia note =#\nx = 1\n", "julia note"),
    "a.vb": ("' basic note\nDim x = 1\n", "' basic note"),
    "a.graphql": ('"""graphql note"""\ntype Q { a: Int }\n', "graphql note"),
    "a.erl": ("% erlang note\nf() -> ok.\n", "% erlang note"),
    "a.tex": ("% tex note\n\\section{A}\n", "% tex note"),
    "a.f90": ("! fortran note\nprogram p\nend program p\n", "! fortran note"),
    "a.ml": ("(* ocaml note *)\nlet x = 1\n", "ocaml note"),
    "a.nix": ("# nix note\n{ }\n", "# nix note"),
    "a.zig": ("// zig note\nconst x = 1;\n", "// zig note"),
    "a.dart": ("// dart note\nvar x = 1;\n", "// dart note"),
    "a.gradle": ("// gradle note\next.x = 1\n", "// gradle note"),
    "a.sol": ("// solidity note\ncontract C {}\n", "// solidity note"),
    "a.proto": ("// proto note\nmessage M {}\n", "// proto note"),
    "a.r": ("# r note\nx <- 1\n", "# r note"),
    "a.ini": ("; ini note\n[x]\n", "; ini note"),
    "a.m": ("// objc note\nint x = 1;\n", "// objc note"),
    "a.jsonc": ('{"a": 1} // jsonc note\n', "// jsonc note"),
    "a.fish": ("# fish note\nset x 1\n", "# fish note"),
    "a.bicep": ("// bicep note\nparam x int\n", "// bicep note"),
    "a.xsl": ("<!-- xsl note -->\n<x/>\n", "xsl note"),
    ".env": ("# env note\nX=1\n", "# env note"),
    ".gitignore": ("# gitignore note\n*.log\n", "# gitignore note"),
    "CMakeLists.txt": ("# cmake note\nproject(p)\n", "# cmake note"),
    "Rakefile": ("# rakefile note\ntask :default\n", "# rakefile note"),
}


class LanguageTable(HookCase):
    def test_every_language_in_the_table_reports_its_comment(self):
        self.write("seed.txt", "seed\n")
        self.commit()
        self.session_start()
        for name, (body, _) in SAMPLES.items():
            self.write(name, body)
        added = self.bash("wrote one file per language")
        self.assertEqual(sorted(added), sorted(want for _, want in SAMPLES.values()))


class ShellCommands(HookCase):
    def test_bash_writing_a_comment_reports_it(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.session_start()
        self.write("a.py", "x = 1\n# written by a heredoc\n")
        self.assertEqual(self.bash("cat > a.py"), ["# written by a heredoc"])

    def test_first_bash_of_a_session_is_not_a_blind_spot(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.session_start()
        self.write("a.py", "x = 1\n# the very first command wrote this\n")
        self.assertEqual(
            self.bash("sed -i ..."), ["# the very first command wrote this"]
        )

    def test_a_comment_is_reported_once_and_not_again(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.session_start()
        self.write("a.py", "x = 1\n# landed once\n")
        self.assertEqual(self.bash(), ["# landed once"])
        self.assertEqual(self.bash(), [])

    def test_bash_without_a_session_start_seeds_silently(self):
        self.write("a.py", "x = 1\n# pre-existing\n")
        self.commit("base")
        self.write("a.py", "x = 1\n# pre-existing\n# uncommitted\n")
        self.assertEqual(self.bash(), [])


class Regressions(HookCase):
    """Each of these reported a comment the user had already decided on."""

    def test_edit_before_bash_does_not_dump_the_whole_working_tree(self):
        self.write("a.py", "def g():\n    return 1\n")
        self.commit()
        self.write("a.py", "def g():\n    # decided on yesterday\n    return 1\n")
        self.session_start()
        path = os.path.join(self.repo, "a.py")
        self.assertEqual(self.edit(path, "return 1", "# brand new\n    return 1"), ["# brand new"])
        self.assertEqual(self.bash("echo hello"), [])

    def test_untracked_file_nobody_touched_is_not_reported(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.write("unrelated.js", "// an untracked file from last week\nconst k = 1\n")
        self.session_start()
        self.assertEqual(self.bash("echo hello"), [])

    def test_rewriting_a_file_does_not_re_report_its_existing_comments(self):
        body = "def g():\n    # a human wrote this, uncommitted\n    return 1\n"
        self.write("a.py", "def g():\n    return 1\n")
        self.commit()
        self.write("a.py", body)
        self.session_start()
        self.assertEqual(self.write_tool(os.path.join(self.repo, "a.py"), body), [])

    def test_rewriting_a_file_still_reports_what_the_rewrite_added(self):
        self.write("a.py", "def g():\n    # already here\n    return 1\n")
        self.commit()
        self.session_start()
        added = self.write_tool(
            os.path.join(self.repo, "a.py"),
            "def g():\n    # already here\n    # newly added\n    return 1\n",
        )
        self.assertEqual(added, ["# newly added"])

    def test_same_comment_text_in_a_second_file_is_still_reported(self):
        self.commit("empty")
        self.session_start()
        first = os.path.join(self.repo, "x.ts")
        second = os.path.join(self.repo, "y.ts")
        self.assertEqual(self.write_tool(first, "// inline note\nexport const a = 1\n"), ["// inline note"])
        self.assertEqual(self.write_tool(second, "// inline note\nexport const b = 2\n"), ["// inline note"])


class SessionOutsideARepo(HookCase):
    """Claude started a level up from the checkout, the way ~/dev holds many."""

    def setUp(self):
        super().setUp()
        self.outside = tempfile.mkdtemp(prefix="comment-guard-outside-")
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

    def test_an_edit_gives_the_shell_path_a_baseline(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.session_start(cwd=self.outside)
        self.edit(os.path.join(self.repo, "a.py"), "x = 1", "x = 2", cwd=self.outside)
        self.write("a.py", "x = 2\n# written by a heredoc\n")
        self.assertEqual(
            self.bash("cat > a.py", cwd=self.outside), ["# written by a heredoc"]
        )

    def test_registering_a_repo_does_not_dump_what_it_already_had(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.write("a.py", "x = 1\n# decided on yesterday\n")
        self.session_start(cwd=self.outside)
        self.edit(os.path.join(self.repo, "a.py"), "x = 1", "x = 2", cwd=self.outside)
        self.assertEqual(self.bash("echo hello", cwd=self.outside), [])

    def test_a_repo_no_tool_has_touched_stays_out_of_scope(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.session_start(cwd=self.outside)
        self.write("a.py", "x = 1\n# written by a heredoc\n")
        self.assertEqual(self.bash("cat > a.py", cwd=self.outside), [])

    def test_a_file_in_no_repository_is_beyond_the_shell_path(self):
        """`git diff HEAD` is the baseline. Without a repository there is none."""
        path = os.path.join(self.outside, "loose.py")
        self.edit(path, "x = 1", "# from a tool edit\nx = 1", cwd=self.outside)
        with open(path, "w") as handle:
            handle.write("# from a shell command\nx = 1\n")
        self.assertEqual(self.bash("cat > loose.py", cwd=self.outside), [])


class SecondRepository(HookCase):
    """A shell command landing in a checkout other than the one it started in."""

    def setUp(self):
        super().setUp()
        self.other = tempfile.mkdtemp(prefix="comment-guard-other-")
        self.addCleanup(shutil.rmtree, self.other, ignore_errors=True)
        self.git("init", "-q", ".", repo=self.other)
        self.git("config", "user.email", "t@example.com", repo=self.other)
        self.git("config", "user.name", "Test", repo=self.other)

    def test_an_edit_there_brings_it_into_scope(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.write("b.py", "y = 1\n", repo=self.other)
        self.commit(repo=self.other)
        self.session_start()
        self.edit(os.path.join(self.other, "b.py"), "y = 1", "y = 2")
        self.write("b.py", "y = 2\n# sed -i wrote this\n", repo=self.other)
        self.assertEqual(self.bash("sed -i ..."), ["# sed -i wrote this"])

    def test_the_session_repo_is_still_watched_alongside_it(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.write("b.py", "y = 1\n", repo=self.other)
        self.commit(repo=self.other)
        self.session_start()
        self.edit(os.path.join(self.other, "b.py"), "y = 1", "y = 2")
        self.write("a.py", "x = 1\n# and one here\n")
        self.assertEqual(self.bash("sed -i ..."), ["# and one here"])


class StateHandling(HookCase):
    def test_corrupt_state_file_does_not_break_the_hook(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.session_start()
        for name in os.listdir(self.state):
            with open(os.path.join(self.state, name), "w") as handle:
                handle.write("{ not json")
        path = os.path.join(self.repo, "a.py")
        self.assertEqual(self.edit(path, "x = 1", "# still works\nx = 1"), ["# still works"])

    def test_sessions_do_not_share_a_baseline(self):
        self.write("a.py", "x = 1\n")
        self.commit()
        self.session_start()
        self.write("a.py", "x = 1\n# landed\n")
        self.assertEqual(self.bash(), ["# landed"])
        self.session = "a-different-session"
        self.assertEqual(self.bash(), [])


if __name__ == "__main__":
    unittest.main()
