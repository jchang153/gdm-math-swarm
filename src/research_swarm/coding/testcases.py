"""Host-side parsing of a task's tests.py (ast only; never executed on the host).

The parsing itself lives in ``runner.parse_tests`` so the agents' run_tests.py
(the same harness text) splits tests identically. Semantics (coding-interfaces.md):
the body of ``def check(candidate)`` is split into top-level statements; each
statement containing an ``assert`` is one test (numbered from 1), the others are
setup and run in order; module-level statements other than ``check`` form a prelude.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import runner


@dataclass(frozen=True)
class TestCase:
    index: int                # 1-based test number
    source: str               # statement text as written in tests.py
    lhs: str | None = None    # for `assert X == Y`: X (its actual value is reported)
    rhs: str | None = None


@dataclass(frozen=True)
class ParsedTests:
    prelude: str
    param: str
    steps: tuple              # raw harness steps (tests and setup), in order

    @property
    def tests(self) -> list[TestCase]:
        return [TestCase(s['index'], s['source'], s.get('lhs'), s.get('rhs'))
                for s in self.steps if s.get('index') is not None]

    @property
    def total(self) -> int:
        return sum(1 for s in self.steps if s.get('index') is not None)

    def as_harness(self) -> dict:
        return {'prelude': self.prelude, 'param': self.param, 'steps': [dict(s) for s in self.steps]}


def parse(test_source: str) -> ParsedTests:
    """Parse a tests.py text. Raises ValueError (or SyntaxError) for a file without tests."""
    parsed = runner.parse_tests(test_source)
    return ParsedTests(parsed['prelude'], parsed['param'], tuple(parsed['steps']))


def count_tests(test_source: str) -> int:
    return parse(test_source).total
