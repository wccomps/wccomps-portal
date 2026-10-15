"""No tracked source or template holds a raw invisible format character (Trojan Source, CVE-2021-42574).

Bidi controls reorder how code reads on screen, and zero-width characters hide in names and strings, so a
reviewer can't see what the code does. Write such a character as a \\u escape. A file that must hold one
raw goes in EXCEPTIONS with the reason.
"""

import subprocess
import unicodedata

from core.tests.test_no_unused_code import REPO_DIR, Unused, fail_with

CHECKED_SUFFIXES = (".py", ".html", ".txt", ".js", ".css", ".md", ".toml", ".yml", ".yaml")
EXCEPTIONS: dict[str, str] = {}


def invisible_characters() -> list[Unused]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    found: list[Unused] = []
    for path in tracked:
        if not path.endswith(CHECKED_SUFFIXES) or not (REPO_DIR / path).is_file():
            continue
        text = (REPO_DIR / path).read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if hidden := [f"U+{ord(c):04X}" for c in line if unicodedata.category(c) == "Cf"]:
                found.append(Unused(path, f"line {line_number}: {', '.join(hidden)}"))
    return found


def test_no_tracked_file_holds_a_raw_invisible_format_character() -> None:
    fail_with("Raw invisible format characters (write them as \\u escapes)", invisible_characters(), EXCEPTIONS)
