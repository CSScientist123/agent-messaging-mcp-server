"""Manual, live smoke test for the three real adapters.

This script requires actually-running remotes: a real `nlm` CLI logged into
a real Google account with at least one notebook and source, a real Claude
Science server listening on its configured base URL with a valid session
cookie, and a real `tmux` + `agy` setup with a live conversation session.

It is NOT part of the automated test suite. It is never collected or run by
pytest -- it has no `test_*` functions and everything it does lives behind
`if __name__ == "__main__":`. Run it by hand, and only when you actually
want to hit those live services:

    python3 adapters/live_smoke.py \\
        --nlm-notebook <notebook_id> --nlm-source <source_id> \\
        --science-project <project_id> --science-frame <frame_id> \\
        --agy-folder <folder_path> --agy-conversation <conversation_id>

Every step is independent and best-effort: one adapter's failure does not
stop the others from being tried. Each step prints a single PASS/FAIL line
with a short reason, and the script exits non-zero if anything failed.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass, field


@dataclass
class Results:
    passed: int = 0
    failed: int = 0
    lines: list[str] = field(default_factory=list)

    def record(self, label: str, ok: bool, detail: str = "") -> None:
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {label}" + (f" -- {detail}" if detail else "")
        self.lines.append(line)
        print(line)
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    def step(self, label: str):
        return _Step(self, label)


class _Step:
    """Context manager: records PASS if the block completes, FAIL with the
    exception's message otherwise. Never re-raises -- one failing step must
    not stop the rest of the smoke test from running."""

    def __init__(self, results: Results, label: str) -> None:
        self.results = results
        self.label = label

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.results.record(self.label, True)
        else:
            detail = "".join(traceback.format_exception_only(exc_type, exc)).strip()
            self.results.record(self.label, False, detail)
        return True  # swallow the exception either way


def run_notebooklm_smoke(results: Results, notebook_id: str, source_id: str) -> None:
    from adapters.notebooklm.adapter import NotebookLMExtension

    ext = NotebookLMExtension(harvest_wait_seconds=10.0)

    with results.step("notebooklm: verify_project_system_id"):
        ok = ext.verify_project_system_id(notebook_id)
        assert ok, f"notebook {notebook_id!r} was not found by the real nlm CLI"

    with results.step("notebooklm: verify_partner_id_in_remote"):
        ok = ext.verify_partner_id_in_remote(notebook_id, source_id)
        assert ok, f"source {source_id!r} was not listed under notebook {notebook_id!r}"

    with results.step("notebooklm: deliver_message + read_remote_result round trip"):
        ext.deliver_message(
            partner_id_in_remote=source_id,
            behavior="[QUERY]",
            body="In one sentence, what is this source about?",
        )
        assert ext.poll_completion(partner_id_in_remote=source_id) is True
        answer = ext.read_remote_result(partner_id_in_remote=source_id)
        assert answer, "read_remote_result returned an empty answer"

    with results.step("notebooklm: stop/resume refuse (NonExecutingExtension)"):
        from messaging_core.errors import Rejected

        try:
            ext.stop_remote_execution(partner_id_in_remote=source_id, reason="smoke test")
            raise AssertionError("stop_remote_execution did not refuse")
        except Rejected:
            pass


def run_claude_science_smoke(results: Results, project_id: str, frame_id: str) -> None:
    from adapters.claude_science.adapter import ClaudeScienceExtension

    ext = ClaudeScienceExtension()

    with results.step("claude_science: verify_project_system_id"):
        ok = ext.verify_project_system_id(project_id)
        assert ok, f"project {project_id!r} was not found (check base URL / cookie)"

    with results.step("claude_science: verify_partner_id_in_remote"):
        ok = ext.verify_partner_id_in_remote(project_id, frame_id)
        assert ok, f"frame {frame_id!r} was not found under project {project_id!r}"

    with results.step("claude_science: poll_completion"):
        # Just confirm the call succeeds and returns a bool -- the frame's
        # actual status is whatever it happens to be right now.
        value = ext.poll_completion(partner_id_in_remote=frame_id)
        assert isinstance(value, bool)


def run_antigravity_smoke(results: Results, folder: str, conversation_id: str) -> None:
    from adapters.antigravity.adapter import AntigravityExtension

    ext = AntigravityExtension()

    with results.step("antigravity: verify_project_system_id (folder is a dir)"):
        ok = ext.verify_project_system_id(folder)
        assert ok, f"{folder!r} is not a directory"

    with results.step("antigravity: verify_partner_id_in_remote (tmux session exists)"):
        ok = ext.verify_partner_id_in_remote(folder, conversation_id)
        assert ok, f"no tmux session found for conversation {conversation_id!r}"

    with results.step("antigravity: poll_completion"):
        value = ext.poll_completion(partner_id_in_remote=conversation_id)
        assert isinstance(value, bool)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nlm-notebook")
    parser.add_argument("--nlm-source")
    parser.add_argument("--science-project")
    parser.add_argument("--science-frame")
    parser.add_argument("--agy-folder")
    parser.add_argument("--agy-conversation")
    args = parser.parse_args()

    results = Results()

    if args.nlm_notebook and args.nlm_source:
        run_notebooklm_smoke(results, args.nlm_notebook, args.nlm_source)
    else:
        print("[SKIP] notebooklm -- pass --nlm-notebook and --nlm-source to run it")

    if args.science_project and args.science_frame:
        run_claude_science_smoke(results, args.science_project, args.science_frame)
    else:
        print("[SKIP] claude_science -- pass --science-project and --science-frame to run it")

    if args.agy_folder and args.agy_conversation:
        run_antigravity_smoke(results, args.agy_folder, args.agy_conversation)
    else:
        print("[SKIP] antigravity -- pass --agy-folder and --agy-conversation to run it")

    print(f"\n{results.passed} passed, {results.failed} failed")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
