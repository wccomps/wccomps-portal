"""No tracked source or template holds a raw invisible character or unusual space (Trojan Source, CVE-2021-42574).

Bidi controls reorder how code reads on screen, zero-width characters and blank-rendering letters hide in names and
strings, and line and paragraph separators, line-breaking controls and other spaces read as an ordinary space or line
break, so a reviewer can't see what the code does. Write such a character as a \\u escape. A file that must hold
one raw goes in EXCEPTIONS with the reason.
"""

import subprocess
import unicodedata

from core.tests.test_no_unused_code import REPO_DIR, Unused, fail_with

CHECKED_SUFFIXES = (".py", ".html", ".txt", ".js", ".css", ".md", ".toml", ".yml", ".yaml")
EXCEPTIONS: dict[str, str] = {}


# Characters that render as nothing at all though they aren't spaces or controls
BLANK_LOOKING = {"\u115f", "\u1160", "\u3164", "\uffa0", "\u2800"}


def _hidden(character: str) -> bool:
    """A format character, a control other than tab (vertical tab, form feed and NEL included, which editors break
    lines at), a line or paragraph separator, a space other than the ordinary one, or a blank-rendering letter."""
    category = unicodedata.category(character)
    if category == "Cc":
        return character != "\t"
    return category in ("Cf", "Zl", "Zp") or (category == "Zs" and character != " ") or character in BLANK_LOOKING


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
        # Split on newlines only: splitlines() also splits on several characters this guard flags, hiding them
        for line_number, line in enumerate(text.split("\n"), start=1):
            if hidden := [f"U+{ord(c):04X}" for c in line if _hidden(c)]:
                found.append(Unused(f"{path}:{line_number}", ", ".join(hidden)))
    return found


def test_no_tracked_file_holds_a_raw_invisible_character() -> None:
    fail_with("Raw invisible characters (write them as \\u escapes)", invisible_characters(), EXCEPTIONS)


def test_the_guard_flags_every_kind_of_hidden_character() -> None:
    flagged = [
        "\u202e",
        "\u200b",
        "\u2028",
        "\u2029",
        "\u00a0",
        "\u3000",
        "\x0b",
        "\x0c",
        "\x85",
        "\u3164",
        "\u115f",
        "\u2800",
    ]
    assert all(_hidden(c) for c in flagged), [f"U+{ord(c):04X}" for c in flagged if not _hidden(c)]
    assert not any(_hidden(c) for c in (" ", "\t", "a", "\u00e9"))
