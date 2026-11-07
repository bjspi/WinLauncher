"""Write a release's notes from the commits since the previous release.

The release workflow publishes them as the text of the GitHub release. Preview them
with `python tools/release_notes.py v0.16.1`.

The commit `Release X.Y.Z: <summary>` gives the summary at the top; every other
commit since the previous tag follows with its subject and its description.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAGS = "v*"
RELEASE_SUBJECT = re.compile(r"Release \d+\.\d+\.\d+(?::\s*(?P<summary>.*))?")
SKIP_CI = "[skip ci]"
GIT_TIMEOUT_SECONDS = 60
FIELD_END, RECORD_END = "\x1f", "\x1e"  # Never in a commit message.
BODY_INDENT = "  "  # Nests a commit's own list under its subject.


@dataclass(frozen=True, slots=True)
class Commit:
    subject: str
    body: str

    @property
    def release_summary(self) -> str | None:
        """The summary of a `Release X.Y.Z` commit; None for any other commit."""
        match = RELEASE_SUBJECT.fullmatch(self.subject)
        if match is None:
            return None
        return match["summary"] or ""


def previous_tag(tag: str, repository: Path = ROOT) -> str | None:
    """The release tag before `tag`; None for the first release."""
    found = _git(repository, "describe", "--tags", "--abbrev=0", "--match", RELEASE_TAGS, f"{tag}^")
    return found.strip() if found is not None else None


def commits_since(previous: str | None, tag: str, repository: Path = ROOT) -> list[Commit]:
    """The commits `tag` adds to `previous`, oldest first."""
    revisions = f"{previous}..{tag}" if previous else tag
    log = _git(
        repository,
        "log",
        "--reverse",
        "--no-merges",
        f"--format=%s{FIELD_END}%b{RECORD_END}",
        revisions,
    )
    if log is None:
        raise SystemExit(f"Git cannot list the commits of {revisions}.")
    commits = []
    for record in log.split(RECORD_END):
        subject, _, body = record.strip("\n").partition(FIELD_END)
        if subject.strip():
            commits.append(Commit(_without_skip_ci(subject), body.strip()))
    return commits


def release_notes(
    tag: str, previous: str | None, commits: list[Commit], github_repository: str = ""
) -> str:
    """Markdown: the release's summary, then each change with its description."""
    releases = [c for c in commits if c.release_summary is not None]
    changes = [c for c in commits if c.release_summary is None]
    parts = [_summary(c) for c in releases if _summary(c)]
    if changes:
        parts.append("## Changes\n\n" + "\n\n".join(_change(commit) for commit in changes))
    if previous and github_repository:
        compare = f"https://github.com/{github_repository}/compare/{previous}...{tag}"
        parts.append(f"**Full Changelog**: {compare}")
    return "\n\n".join(parts) + "\n"


def _summary(release: Commit) -> str:
    summary = release.release_summary or ""
    lead = summary[:1].upper() + summary[1:]
    return "\n\n".join(part for part in (lead, release.body) if part)


def _change(commit: Commit) -> str:
    if not commit.body:
        return f"- **{commit.subject}**"
    body = "\n".join(BODY_INDENT + line if line else "" for line in commit.body.splitlines())
    return f"- **{commit.subject}**\n\n{body}"


def _without_skip_ci(subject: str) -> str:
    return " ".join(subject.replace(SKIP_CI, "").split())


def _git(repository: Path, *arguments: str) -> str | None:
    """Git's output, or None when it fails, such as `describe` before the first tag."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the release tag, such as v0.16.1")
    parser.add_argument("--repository", default="", help="owner/name for the compare link")
    arguments = parser.parse_args(argv)
    previous = previous_tag(arguments.tag)
    commits = commits_since(previous, arguments.tag)
    notes = release_notes(arguments.tag, previous, commits, arguments.repository)
    sys.stdout.buffer.write(notes.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
