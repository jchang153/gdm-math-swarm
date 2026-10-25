"""The intentionally weak conference-judge keyword stage.

This reproduces the agreed text policy, not mathematical verification. Source
integrity and Lean compilation must follow before operational acceptance.
"""

from dataclasses import dataclass


FORBIDDEN_KEYWORDS: tuple[str, ...] = ("axiom", "sorry", "macro", "syntax")
POLICY_ID = "literal-case-sensitive-whole-source-v1"


@dataclass(frozen=True)
class KeywordFilterResult:
    """A keyword-stage outcome, never a proof-validity judgment."""

    matched_keywords: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.matched_keywords


def check_keywords(source: str) -> KeywordFilterResult:
    """Scan the complete submitted source, including comments and identifiers.

    Matching is case-sensitive literal substring matching. It deliberately does
    not tokenize Lean, strip comments, or enforce additional prompt prohibitions.
    Each matched keyword is returned once in fixed policy order.
    """
    return KeywordFilterResult(
        matched_keywords=tuple(
            keyword for keyword in FORBIDDEN_KEYWORDS if keyword in source
        )
    )


def main() -> int:
    """Inspect one local UTF-8 source file without running Lean or an agent."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Submitted Lean source file")
    args = parser.parse_args()
    try:
        source = args.source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        parser.error(f"Cannot read UTF-8 source: {error}")
    result = check_keywords(source)
    print(
        json.dumps(
            {
                "stage": "keyword_filter",
                "policy": POLICY_ID,
                "passed": result.passed,
                "matched_keywords": result.matched_keywords,
            }
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
