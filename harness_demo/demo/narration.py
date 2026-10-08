"""Presenter output: banners, beat headers, and the pass/fail check.

This is the one place that is allowed to print. The text for each beat is
DATA (BeatSpec), kept apart from the logic in beats.py, so a presenter can
reword a slide without touching a gate.
"""
import textwrap
from dataclasses import dataclass

WIDTH = 74
_BODY = 70


class DemoCheckFailed(AssertionError):
    """A guarantee the demo exists to show did not hold.

    An explicit exception rather than `assert`, because asserts vanish
    under `python -O` and a gate that quietly stops gating is the whole
    nightmare.
    """


def check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoCheckFailed(message)


@dataclass(frozen=True)
class BeatSpec:
    number: int
    name: str          # short label for the scorecard
    title: str         # the question the beat answers
    tests: str         # WHAT WE TEST
    how: str           # HOW IT HOLDS
    watch: str         # WHAT TO WATCH FOR
    takeaway: str      # one line to leave the audience with


def say(text: str = "", indent: int = 2) -> None:
    """Print wrapped prose at a fixed indent."""
    pad = " " * indent
    print(textwrap.fill(text, width=_BODY + indent, initial_indent=pad,
                        subsequent_indent=pad) if text else "")


def banner(title: str, lines: list[str]) -> None:
    print("\n" + "#" * WIDTH)
    print(f"#  {title}")
    print("#")
    for line in lines:
        print(f"#  {line}" if line else "#")
    print("#" * WIDTH)


def beat_header(spec: BeatSpec, pause: bool = False) -> None:
    print("\n" + "=" * WIDTH)
    print(f"  BEAT {spec.number}  |  {spec.title}")
    print("=" * WIDTH)
    for label, text in (("WHAT WE TEST", spec.tests),
                        ("HOW IT HOLDS", spec.how),
                        ("WATCH FOR", spec.watch)):
        say(f"{label}: {text}")
    print()
    if pause:
        input("  [Enter] to run this beat... ")
        print()


def takeaway(spec: BeatSpec) -> None:
    print("\n  " + "-" * (_BODY - 2))
    say(f"TAKEAWAY: {spec.takeaway}")


def section(title: str) -> None:
    print(f"\n----- {title} -----")
