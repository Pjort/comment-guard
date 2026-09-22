#!/usr/bin/env python3
"""Both hooks answer the same events, so they meet on every one of them.

Run with: python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HOOKS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins",
    "comment-guard",
    "hooks",
)
COMMENTS = os.path.join(HOOKS, "comment_guard.py")
DOCS = os.path.join(HOOKS, "doc_guard.py")
SECTION = "\n".join(f"Line {n} of a section nobody asked for." for n in range(1, 16))


class BothHooks(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="both-guard-repo-")
        self.state = tempfile.mkdtemp(prefix="both-guard-state-")
        self.git("init", "-q", ".")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "Test")
        self.write("README.md", "# Tool\n\nRun it.\n")
        self.write("a.py", "def f():\n    return 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "wip")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def git(self, *args):
        subprocess.run(
            ["git", "-C", self.repo, *args], capture_output=True, text=True, check=False
        )

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def payload(self, event, tool=None, tool_input=None):
        body = {"session_id": "shared", "cwd": self.repo, "hook_event_name": event}
        if tool:
            body["tool_name"] = tool
            body["tool_input"] = tool_input or {}
        return json.dumps(body)

    def together(self, event, tool=None, tool_input=None):
        """Both hooks on one event at once, as Claude Code runs them."""
        text = self.payload(event, tool, tool_input)
        env = {**os.environ, "COMMENT_GUARD_STATE_DIR": self.state}
        running = [
            subprocess.Popen(
                ["python3", hook],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for hook in (COMMENTS, DOCS)
        ]
        out = []
        for process in running:
            stdout, stderr = process.communicate(text, timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            out.append(stdout.strip())
        return out

    def context(self, stdout):
        if not stdout:
            return ""
        emitted = json.loads(stdout)
        if "reason" in emitted:
            return emitted["reason"]
        return emitted["hookSpecificOutput"]["additionalContext"]

    def test_one_event_reaches_both_hooks_without_either_losing_its_state(self):
        self.together("SessionStart")
        with open(os.path.join(self.repo, "README.md"), "a") as handle:
            handle.write(f"\n## Notes\n\n{SECTION}\n")
        with open(os.path.join(self.repo, "a.py"), "a") as handle:
            handle.write("# why this exists\n")

        comments, docs = self.together("PostToolUse", "Bash", {"command": "true"})
        self.assertIn("# why this exists", self.context(comments))
        self.assertIn("README.md", self.context(docs))
        self.assertNotIn("README.md", self.context(comments))
        self.assertNotIn("# why this exists", self.context(docs))

    def test_each_hook_keeps_its_own_state_file(self):
        self.together("SessionStart")
        written = sorted(n.split("-")[0] for n in os.listdir(self.state))
        self.assertEqual(written, ["comments", "docs"])
        for name in os.listdir(self.state):
            with open(os.path.join(self.state, name)) as handle:
                self.assertIn("repos", json.load(handle))

    def test_neither_hook_is_disturbed_by_the_other_running_on_every_event(self):
        self.together("SessionStart")
        path = os.path.join(self.repo, "README.md")
        for _ in range(3):
            with open(path, "a") as handle:
                handle.write(f"\n{SECTION}\n")
            self.together("PostToolUse", "Bash", {"command": "true"})
        comments, docs = self.together("PostToolUse", "Bash", {"command": "true"})
        self.assertEqual(self.context(comments), "")
        self.assertEqual(self.context(docs), "")


if __name__ == "__main__":
    unittest.main()
