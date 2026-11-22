"""Print the CI tier for the changes introduced by a pull request."""

import subprocess
import sys

DOC_FILES = {
    b"README.md",
    b"CHANGELOG.md",
    b"AGENTS.md",
    b"CLAUDE.md",
}


def check_level(base: str, head: str) -> str:
    if not base:
        return "full"

    # Treat renames as deletions and additions so both paths count.
    changed = subprocess.check_output(
        ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...{head}", "--"]
    )
    paths = changed.split(b"\0")
    if paths.pop() != b"":
        raise ValueError("incomplete git diff output")
    if paths and all(path.startswith(b"docs/") or path in DOC_FILES for path in paths):
        return "docs"
    return "full"


if __name__ == "__main__":
    print(check_level(*sys.argv[1:]))
