"""Weak operational judge. Acceptance never implies intended-task validity.

Three checks, in order, and nothing else (approved baseline; see docs/judges.md):
1. literal case-sensitive keyword scan of the whole source (keyword_filter);
2. UTF-8 byte equality with the template outside the two editable regions
   (and, for answer-based tasks, outside the extracted answer span);
3. Lean compilation: exit status 0 and zero `declaration uses 'sorry'` warnings.
Known accepted weaknesses are documented, not enforced against.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
from .keyword_filter import check_keywords
from .lean import CompileResult

ACCEPTED_REASON = 'Submission accepted.'
ADMISSION_REASON = 'Lean admission count nonzero or unavailable'


@dataclass(frozen=True)
class TaskTemplate:
    source: str
    theorem_name: str
    answer_based: bool = False
    # Not read by the judge. The independent evaluator sets it on a template
    # whose answer field already holds a resolved value (evaluator.canonical_challenge).
    answer_value_known: bool = False


@dataclass(frozen=True)
class JudgeResult:
    accepted: bool
    stage: str
    reason: str
    compile_result: CompileResult | None = None


def editable_spans(source):
    """Marker boundaries remain immutable; only bytes between them can change."""
    pairs = [('EDITABLE START', 'EDITABLE END'),
             ('PREAMBLE START', 'PREAMBLE END'), ('PROOF START', 'PROOF END')]
    spans = []
    for start, end in pairs:
        begins = list(re.finditer(r'(?m)^-- ' + start + r'\r?\n', source))
        ends = list(re.finditer(r'(?m)^-- ' + end + r'(?:\r?\n|$)', source))
        if len(begins) != len(ends):
            raise ValueError('Unpaired editable markers')
        for a, b in zip(begins, ends):
            if a.end() > b.start():
                raise ValueError('Invalid editable markers')
            spans.append((a.end(), b.start()))
    spans.sort()
    if len(spans) != 2 or any(a[1] > b[0] for a,b in zip(spans,spans[1:])):
        raise ValueError('Exactly two disjoint editable regions required')
    return spans


# Intentional paper reconstruction, NOT a balanced-parentheses parser: the
# FIRST match anywhere in the file (preamble and comments included), with a
# lazy span that ends at the first `)`. `\b` and `\s*` admit `.answer(` and
# `answer (`; DOTALL lets the span cross lines.
ANSWER_PATTERN = re.compile(r'\banswer\s*\((.*?)\)', re.DOTALL)


def answer_span(source):
    match = ANSWER_PATTERN.search(source)
    if not match:
        raise ValueError('Missing answer field')
    return match.span(1)


def immutable_segments(source, answer_based=False):
    spans = editable_spans(source)
    if answer_based:
        ans = answer_span(source)
        if not any(a <= ans[0] and ans[1] <= b for a,b in spans):
            spans.append(ans)
    spans.sort()
    previous = 0
    segments = []
    for a,b in spans:
        segments.append(source[previous:a].encode('utf-8'))
        previous = b
    segments.append(source[previous:].encode('utf-8'))
    return segments


def judge_submission(template: TaskTemplate, source: str, compiler) -> JudgeResult:
    keywords = check_keywords(source)
    if not keywords.passed:
        return JudgeResult(False, 'keyword_filter', 'Forbidden text: ' + ', '.join(keywords.matched_keywords))
    try:
        intact = immutable_segments(template.source, template.answer_based) == immutable_segments(source, template.answer_based)
    except ValueError as exc:
        return JudgeResult(False, 'source_integrity', str(exc))
    if not intact:
        return JudgeResult(False, 'source_integrity', 'Immutable source differs')
    result = compiler.compile(source)
    if not result.succeeded:
        reason = 'Lean ' + result.status
        if result.status == 'success':
            reason = ADMISSION_REASON
        return JudgeResult(False, 'compilation', reason, result)
    return JudgeResult(True, 'accepted', ACCEPTED_REASON, result)
