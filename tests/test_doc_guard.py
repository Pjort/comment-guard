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
    "doc_guard.py",
)

SECTION = "\n".join(f"Line {n} of a section nobody asked for." for n in range(1, 16))


class HookCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="doc-guard-repo-")
        self.state = tempfile.mkdtemp(prefix="doc-guard-state-")
        self.session = "test-session"
        self.git("init", "-q", ".")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "Test")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def git(self, *args):
        subprocess.run(
            ["git", "-C", self.repo, *args], capture_output=True, text=True, check=False
        )

    def commit(self, message="wip"):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def fire(self, event, tool=None, tool_input=None, cwd=None):
        """Run the hook and return what it reported, or "" when it stayed silent."""
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
            return ""
        return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]

    def session_start(self, cwd=None):
        return self.fire("SessionStart", cwd=cwd)

    def edit(self, path, old, new):
        with open(path) as handle:
            body = handle.read()
        with open(path, "w") as handle:
            handle.write(body.replace(old, new, 1))
        return self.fire(
            "PostToolUse", "Edit", {"file_path": path, "old_string": old, "new_string": new}
        )

    def write_tool(self, path, content):
        self.fire("PreToolUse", "Write", {"file_path": path, "content": content})
        with open(path, "w") as handle:
            handle.write(content)
        return self.fire("PostToolUse", "Write", {"file_path": path, "content": content})

    def bash(self, command="true"):
        return self.fire("PostToolUse", "Bash", {"command": command})

    def shell(self, script):
        subprocess.run(["bash", "-c", script], cwd=self.repo, capture_output=True, check=False)


class Appending(HookCase):
    def test_a_section_appended_to_a_doc_is_reported(self):
        path = self.write("README.md", "# Tool\n\n## Install\n\nRun it.\n")
        self.commit()
        report = self.edit(path, "Run it.", f"Run it.\n\n## Notes\n\n{SECTION}")
        self.assertIn("grew without replacing anything", report)
        self.assertIn("README.md", report)

    def test_an_edit_that_replaces_as_much_as_it_adds_is_silent(self):
        path = self.write("README.md", "# Tool\n\n" + SECTION + "\n")
        self.commit()
        rewritten = "\n".join(f"Rewritten line {n}." for n in range(1, 18))
        self.assertEqual(self.edit(path, SECTION, rewritten), "")

    def test_a_small_addition_is_silent(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.assertEqual(self.edit(path, "Run it.", "Run it.\n\nIt takes one flag.\n"), "")

    def test_a_large_rewrite_that_ends_up_longer_is_still_a_rewrite(self):
        old = "\n".join(f"Old line {n}." for n in range(1, 31))
        path = self.write("README.md", f"# Tool\n\n{old}\n")
        self.commit()
        new = "\n".join(f"New line {n}." for n in range(1, 46))
        self.assertEqual(self.edit(path, old, new), "")

    def test_an_append_that_also_tweaks_a_line_is_still_appending(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n\nOne flag.\n")
        self.commit()
        report = self.edit(path, "One flag.", f"Two flags.\n\n## Notes\n\n{SECTION}")
        self.assertIn("grew without replacing anything", report)

    def test_blank_lines_are_not_content(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        spaced = "\n\n".join(f"Paragraph {n}." for n in range(1, 9))
        self.assertEqual(self.edit(path, "Run it.", f"Run it.\n\n{spaced}\n"), "")

    def test_a_write_over_an_uncommitted_doc_is_measured_against_the_disk(self):
        body = "# Guide\n\n" + "\n".join(f"Paragraph {n}." for n in range(1, 21))
        path = self.write("GUIDE.md", body + "\n")
        report = self.write_tool(path, body + f"\n\n## Notes\n\n{SECTION}\n")
        self.assertIn("grew without replacing anything", report)

    def test_a_doc_being_born_is_not_appending(self):
        path = os.path.join(self.repo, "GUIDE.md")
        self.assertEqual(self.write_tool(path, f"# Guide\n\n{SECTION}\n"), "")

    def test_a_write_over_an_existing_doc_is_measured_against_it(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        report = self.write_tool(path, f"# Tool\n\nRun it.\n\n## Notes\n\n{SECTION}\n")
        self.assertIn("grew without replacing anything", report)

    def test_multiedit_is_measured_the_same_way(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        report = self.fire(
            "PostToolUse",
            "MultiEdit",
            {
                "file_path": path,
                "edits": [{"old_string": "Run it.", "new_string": f"Run it.\n\n{SECTION}"}],
            },
        )
        self.assertIn("grew without replacing anything", report)


class Narration(HookCase):
    def assert_flags(self, added, expected):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        report = self.edit(path, "Run it.", f"Run it.\n\n{added}")
        if expected:
            self.assertIn(expected, report, report)
            self.assertIn("describe the change rather than the thing", report)
        else:
            self.assertEqual(report, "")

    def test_a_changes_heading_is_narration(self):
        self.assert_flags("## Recent changes\n", "## Recent changes")

    def test_now_verb_is_narration(self):
        self.assert_flags("The parser now handles nested fences.", "now handles")

    def test_previously_is_narration(self):
        self.assert_flags("Previously this required a flag.", "Previously")

    def test_a_dated_bullet_is_narration(self):
        self.assert_flags("- 2026-09-17 reworked the parser", "2026-09-17")

    def test_has_been_updated_is_narration(self):
        self.assert_flags("The table has been updated.", "has been updated")

    def test_first_person_change_is_narration(self):
        self.assert_flags("We renamed the flag to --quiet.", "We renamed")

    def test_naming_the_change_itself_is_narration(self):
        self.assert_flags("This change adds a second hook.", "This change adds")

    def test_added_support_for_is_narration(self):
        self.assert_flags("Added support for nested fences.", "Added support for")

    def test_as_of_this_release_is_narration(self):
        self.assert_flags("As of this release the flag is gone.", "As of this release")

    def test_plain_description_is_not_narration(self):
        self.assert_flags("The parser reads nested fences.", None)

    def test_prose_that_merely_sounds_like_it_is_not_narration(self):
        self.assert_flags(
            "Run it now, then read the output.\n\n"
            "It supports nested fences, and no file longer than 200 KB.\n\n"
            "The update command fetches the repo.\n",
            None,
        )

    def test_a_word_beginning_like_one_is_not_narration(self):
        self.assert_flags("Nowhere does it write to disk.", None)

    def test_narration_inside_a_code_fence_is_not_prose(self):
        self.assert_flags("```\n$ tool --help\nnow uses stdin\n```\n", None)

    def test_narration_in_a_nested_list_item_is_prose(self):
        self.assert_flags("- flags\n    - the parser now handles fences\n", "now handles")

    def test_no_longer_plus_a_behaviour_is_narration(self):
        self.assert_flags("It no longer reads stdin.", "no longer reads")

    def test_no_longer_describing_today_is_not_narration(self):
        self.assert_flags(
            "Purge the blob when no longer needed.\n\n"
            "Keep the file no longer than 200 KB.\n",
            None,
        )

    def test_now_in_front_of_anything_is_narration(self):
        self.assert_flags("It is now formally out of scope.", "is now formally")

    def test_a_renaming_is_narration(self):
        self.assert_flags("- `service.port` renamed as `service.ports.xds`.", "renamed as")

    def test_a_value_moving_is_narration(self):
        self.assert_flags("- `runAsGroup` is changed from `0` to `1001`.", "changed from")

    def test_sequencing_instructions_are_not_narration(self):
        self.assert_flags(
            "Now run the tests.\n\nOpen the file, then edit it.\n\n"
            "Install it now, then read the output.\n",
            None,
        )

    def test_narration_is_reported_however_small_the_edit(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        report = self.edit(path, "Run it.", "Run it. It now reads stdin.")
        self.assertIn("now reads", report)


PASSAGE = (
    "The parser reads nested fences and keeps its state machine small.\n"
    "It handles indented code, tables and list items without special cases.\n"
    "Anything it cannot read is skipped rather than guessed at.\n"
)


class Repetition(HookCase):
    def test_a_heading_the_file_already_has_is_reported(self):
        path = self.write(
            "README.md", "# Tool\n\n## Configuration\n\nSet the flag.\n\n## Usage\n\nRun it.\n"
        )
        self.commit()
        report = self.edit(
            path, "## Usage\n\nRun it.", "## Usage\n\nRun it.\n\n## Configuration\n\nSet another."
        )
        self.assertIn('the heading "configuration" now appears 2 times', report)
        self.assertIn("still sitting there", report)

    def test_a_passage_the_file_already_carries_is_reported(self):
        path = self.write("README.md", f"# Tool\n\n{PASSAGE}\n## Notes\n\nRun it.\n")
        self.commit()
        report = self.edit(path, "Run it.", f"Run it.\n\n{PASSAGE}")
        self.assertIn("is already in the file", report)

    def test_a_reworded_passage_is_still_the_same_passage(self):
        path = self.write("README.md", f"# Tool\n\n{PASSAGE}\n## Notes\n\nRun it.\n")
        self.commit()
        reworded = (
            "The parser reads nested fences, keeping its state machine small.\n"
            "Indented code, tables and list items need no special cases.\n"
            "Anything unreadable is skipped rather than guessed at.\n"
        )
        report = self.edit(path, "Run it.", f"Run it.\n\n{reworded}")
        self.assertIn("is already in the file", report)

    def test_saying_something_new_is_not_repetition(self):
        path = self.write("README.md", f"# Tool\n\n{PASSAGE}\n## Notes\n\nRun it.\n")
        self.commit()
        fresh = (
            "Reports reach Claude as additional context, never the transcript.\n"
            "State lives under the plugin directory and prunes itself weekly.\n"
            "Set an environment variable to move it somewhere else.\n"
        )
        self.assertNotIn("already in the file", self.edit(path, "Run it.", f"Run it.\n\n{fresh}"))

    def test_a_table_repeating_its_header_is_not_repetition(self):
        table = (
            "| Variable | Seen in | Default | Purpose | Owner |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| RETRIES | gateway | three | throttling | platform |\n"
            "| TIMEOUT | notifier | thirty | deadlines | platform |\n"
        )
        path = self.write("README.md", f"# Tool\n\n## A\n\n{table}\n## B\n\nRun it.\n")
        self.commit()
        self.assertNotIn("already in the file", self.edit(path, "Run it.", f"Run it.\n\n{table}"))

    def test_repetition_is_reported_even_when_the_edit_replaced_more_than_it_added(self):
        body = "\n".join(f"Old line {n}." for n in range(1, 31))
        path = self.write("README.md", f"# Tool\n\n{PASSAGE}\n{body}\n")
        self.commit()
        report = self.edit(path, body, PASSAGE)
        self.assertIn("is already in the file", report)


class Shape(HookCase):
    def test_front_matter_is_not_content(self):
        front = "---\ntitle: Guide\ntags: [a, b]\ndate: 2026-09-17\n---\n"
        path = self.write("README.md", f"{front}\n# Tool\n\nRun it.\n")
        self.commit()
        report = self.edit(path, "Run it.", f"Run it.\n\n{SECTION}")
        self.assertIn("now 17 lines", report)

    def test_a_setext_heading_counts_as_a_heading(self):
        body = "\n\n".join(f"Section {n}\n{'-' * 9}\n\nText." for n in range(1, 32))
        path = self.write("README.md", body + "\n")
        self.commit()
        report = self.edit(path, "Section 1", "Section 1\n\nOne more thought.\n\nSection 1")
        self.assertIn("past the length anyone reads", report)

    def test_a_thematic_break_is_not_a_heading(self):
        body = "\n\n".join(f"Text {n}.\n\n---" for n in range(1, 40))
        path = self.write("README.md", "# Tool\n\n" + body + "\n")
        self.commit()
        report = self.edit(path, "Text 1.", "Text 1.\n\nOne more thought.")
        self.assertNotIn("past the length anyone reads", report)


class Scope(HookCase):
    def test_a_changelog_is_not_exempt(self):
        path = self.write("CHANGELOG.md", "# Changelog\n")
        self.commit()
        report = self.edit(path, "# Changelog", f"# Changelog\n\n{SECTION}")
        self.assertIn("CHANGELOG.md", report)

    def test_an_adr_is_not_exempt(self):
        path = self.write("docs/adr/0001-pick-a-queue.md", "# Pick a queue\n")
        self.commit()
        report = self.edit(path, "# Pick a queue", f"# Pick a queue\n\n{SECTION}")
        self.assertIn("docs/adr/0001-pick-a-queue.md", report)

    def test_a_tree_nobody_here_authors_is_skipped(self):
        path = self.write("node_modules/thing/README.md", "# Thing\n\nRun it.\n")
        self.commit()
        self.assertEqual(self.edit(path, "Run it.", f"Run it.\n\n{SECTION}"), "")

    def test_any_markdown_path_is_a_doc(self):
        for name in (
            "docs/architecture.md",
            "CLAUDE.md",
            "notes/2026/planning.markdown",
            "src/components/Button.mdx",
            "deep/nested/guide.mkd",
        ):
            with self.subTest(name=name):
                path = self.write(name, "# Title\n\nRun it.\n")
                self.commit()
                report = self.edit(path, "Run it.", f"Run it.\n\n{SECTION}")
                self.assertIn(name, report)

    def test_two_docs_sharing_a_basename_are_named_apart(self):
        first = self.write("docs/api/auth.md", "# Auth\n\nRun it.\n")
        self.write("docs/web/auth.md", "# Auth\n\nRun it.\n")
        self.commit()
        report = self.edit(first, "Run it.", f"Run it.\n\n{SECTION}")
        self.assertIn("docs/api/auth.md", report)
        self.assertNotIn("docs/web/auth.md", report)

    def test_a_doc_in_a_subdirectory_is_named_from_the_repository_root(self):
        path = self.write("docs/guides/setup.md", "# Setup\n\nRun it.\n")
        self.commit()
        report = self.edit(path, "Run it.", f"Run it.\n\n{SECTION}")
        self.assertIn("This edit to docs/guides/setup.md", report)

    def test_code_is_not_a_doc(self):
        path = self.write("a.py", "x = 1\n")
        self.commit()
        self.assertEqual(self.edit(path, "x = 1", f"x = 1\n'''\n{SECTION}\n'''"), "")

    def test_a_long_file_is_reported_even_on_a_modest_addition(self):
        body = "# Tool\n\n" + "\n".join(f"Paragraph {n}." for n in range(1, 500))
        path = self.write("README.md", body + "\n")
        self.commit()
        report = self.edit(path, "Paragraph 1.", "Paragraph 1.\n\nAnd one more thought.")
        self.assertIn("past the length anyone reads", report)

    def test_a_much_divided_file_is_reported_even_when_it_is_short(self):
        body = "# Tool\n\n" + "\n\n".join(f"## Section {n}\n\nText." for n in range(1, 32))
        path = self.write("README.md", body + "\n")
        self.commit()
        report = self.edit(path, "## Section 1", "## Section 1\n\nAnd one more thought.")
        self.assertIn("past the length anyone reads", report)


class Shell(HookCase):
    def test_a_heredoc_append_is_reported(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.shell(f"cat >> README.md <<'EOF'\n## Notes\n\n{SECTION}\nEOF")
        report = self.bash("cat >> README.md")
        self.assertIn("README.md", report)
        self.assertIn("grew without replacing anything", report)

    def test_an_edit_is_not_raised_again_by_the_next_command(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.assertIn("README.md", self.edit(path, "Run it.", f"Run it.\n\n{SECTION}"))
        self.assertEqual(self.bash(), "")

    def test_growth_already_uncommitted_at_session_start_is_old_news(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.session_start()
        self.assertEqual(self.bash(), "")

    def test_growth_is_raised_again_after_a_commit_takes_the_last_lot(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())
        self.commit("keep it")
        self.assertEqual(self.bash("git commit"), "")
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())

    def test_an_edit_does_not_hide_later_growth_after_a_commit(self):
        path = self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.assertIn("README.md", self.edit(path, "Run it.", f"Run it.\n\n{SECTION}"))
        self.commit("keep it")
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())

    def test_a_second_append_is_reported_once_more(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())
        self.assertEqual(self.bash(), "")
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())

    def test_two_documents_changed_at_once_make_one_readable_report(self):
        self.write("one.md", "# One\n\nRun it.\n")
        self.write("two.md", "# Two\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.shell(f"cat >> one.md <<'EOF'\n{SECTION}\nEOF")
        self.shell(f"cat >> two.md <<'EOF'\n{SECTION}\nEOF")
        report = self.bash()
        self.assertIn("one.md", report)
        self.assertIn("two.md", report)
        self.assertEqual(report.count("Rewrite or cut what does not survive"), 1)

    def test_more_documents_than_are_listed_are_still_counted(self):
        for n in range(20):
            self.write(f"doc{n:02}.md", "# Doc\n\nRun it.\n")
        self.commit()
        self.session_start()
        for n in range(20):
            self.shell(f"cat >> doc{n:02}.md <<'EOF'\n{SECTION}\nEOF")
        report = self.bash()
        self.assertIn("further document(s) changed the same way", report)

    def test_narration_already_raised_is_not_raised_again(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.shell("echo 'The parser now handles fences.' >> README.md")
        self.assertIn("now handles", self.bash())
        self.assertEqual(self.bash(), "")

    def test_growth_cut_back_and_written_again_is_raised_again(self):
        base = "# Tool\n\nRun it.\n"
        self.write("README.md", base)
        self.commit()
        self.session_start()
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())
        self.write("README.md", base + "Line 1 of a section nobody asked for.\n")
        self.assertEqual(self.bash(), "")
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())

    def test_growth_reverted_and_written_again_is_raised_again(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        self.session_start()
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())
        self.shell("git checkout -- README.md")
        self.assertEqual(self.bash("git checkout"), "")
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        self.assertIn("README.md", self.bash())


class Caps(HookCase):
    def state_for(self, repo):
        name = [n for n in os.listdir(self.state) if n.startswith("docs-")][0]
        with open(os.path.join(self.state, name)) as handle:
            return json.load(handle)["repos"].get(repo, {})

    def test_a_document_past_the_prose_read_limit_is_still_measured(self):
        body = "# Huge\n\n" + "\n".join(f"Paragraph {n} of a long document." for n in range(1, 8000))
        path = self.write("HUGE.md", body + "\n")
        self.commit()
        self.assertGreater(os.path.getsize(path), 200_000)
        report = self.edit(path, "Paragraph 1 of", "Paragraph 1 of\n\nOne more thought on")
        self.assertIn("past the length anyone reads", report)

    def test_a_document_too_large_to_measure_makes_no_claim_about_its_length(self):
        body = "# Huge\n\n" + "\n".join(f"Paragraph {n} of a long document." for n in range(1, 130000))
        path = self.write("HUGE.md", body + "\n")
        self.commit()
        self.assertGreater(os.path.getsize(path), 4_000_000)
        report = self.edit(path, "Paragraph 1 of", f"Paragraph 1 of\n\n{SECTION}\n")
        self.assertIn("grew without replacing anything", report)
        self.assertNotIn("The file is now", report)

    def test_writes_that_never_land_do_not_pile_up(self):
        for n in range(12):
            path = self.write(f"doc{n:02}.md", "# Doc\n\nRun it.\n")
            self.fire("PreToolUse", "Write", {"file_path": path, "content": "# Doc\n"})
        name = [n for n in os.listdir(self.state) if n.startswith("docs-")][0]
        with open(os.path.join(self.state, name)) as handle:
            self.assertEqual(len(json.load(handle)["prewrite"]), 8)

    def test_untracked_documents_are_scanned_up_to_the_cap(self):
        for n in range(600):
            self.write(f"docs/{n:04}.md", "# Note\n\nRun it.\n")
        self.session_start()
        self.assertEqual(len(self.state_for(self.repo)), 500)


class SeveralTrees(HookCase):
    def worktree(self):
        path = tempfile.mkdtemp(prefix="doc-guard-wt-")
        shutil.rmtree(path)
        self.git("worktree", "add", "-q", path, "-b", "feature")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def test_a_worktree_is_watched_as_its_own_tree(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        wt = self.worktree()
        self.session_start()
        path = os.path.join(wt, "README.md")
        report = self.edit(path, "Run it.", f"Run it.\n\n{SECTION}")
        self.assertIn("README.md", report)

    def test_two_trees_holding_the_same_name_are_told_apart(self):
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.commit()
        wt = self.worktree()
        self.session_start()
        self.edit(os.path.join(wt, "README.md"), "Run it.", "Run it. Once.")
        self.shell(f"cat >> README.md <<'EOF'\n{SECTION}\nEOF")
        subprocess.run(
            ["bash", "-c", f"cat >> README.md <<'EOF'\n{SECTION}\nEOF"],
            cwd=wt, capture_output=True, check=False,
        )
        report = self.bash()
        self.assertIn(os.path.join(self.repo, "README.md"), report)
        self.assertIn(os.path.join(wt, "README.md"), report)

    def test_a_submodule_is_watched_as_its_own_tree(self):
        inner = tempfile.mkdtemp(prefix="doc-guard-sub-")
        self.addCleanup(shutil.rmtree, inner, ignore_errors=True)
        for args in (("init", "-q", "."), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "Test")):
            subprocess.run(["git", "-C", inner, *args], capture_output=True, check=False)
        with open(os.path.join(inner, "GUIDE.md"), "w") as handle:
            handle.write("# Guide\n\nRun it.\n")
        for args in (("add", "-A"), ("commit", "-qm", "inner")):
            subprocess.run(["git", "-C", inner, *args], capture_output=True, check=False)
        self.write("README.md", "# Tool\n")
        self.commit()
        subprocess.run(
            ["git", "-C", self.repo, "-c", "protocol.file.allow=always",
             "submodule", "add", "-q", inner, "sub"],
            capture_output=True, check=False,
        )
        self.commit("add submodule")
        self.session_start()
        path = os.path.join(self.repo, "sub", "GUIDE.md")
        if not os.path.exists(path):
            self.skipTest("submodules not permitted in this git configuration")
        report = self.edit(path, "Run it.", f"Run it.\n\n{SECTION}")
        self.assertIn("GUIDE.md", report)


class Safety(HookCase):
    def test_a_doc_outside_any_repository_still_reports(self):
        outside = tempfile.mkdtemp(prefix="doc-guard-loose-")
        try:
            path = os.path.join(outside, "README.md")
            self.write_tool(path, "# Tool\n\nRun it.\n")
            report = self.edit(path, "Run it.", f"Run it.\n\n{SECTION}")
            self.assertIn("grew without replacing anything", report)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_a_malformed_payload_is_silent(self):
        done = subprocess.run(
            ["python3", HOOK],
            input="not json",
            capture_output=True,
            text=True,
            env={**os.environ, "COMMENT_GUARD_STATE_DIR": self.state},
            timeout=30,
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
