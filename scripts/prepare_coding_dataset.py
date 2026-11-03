#!/usr/bin/env python3
"""Build the frozen 100-task coding pool (task set ilcb100) from Impossible-LiveCodeBench.

Upstream: HF dataset fjzzq2002/impossible_livecodebench (ImpossibleBench, arXiv:2510.20270,
GitHub safety-research/impossiblebench), pinned to UPSTREAM_REVISION. The three splits share
the 103 base tasks lcbhard_<n>; `original` carries the correct tests, `oneoff` changes the
expected value of exactly one assert, `conflicting` adds an assert that repeats an earlier input
with a different expected value.

The raw directory is untrusted data and is never executed: tests and prompts are parsed with
`ast` only. It must contain <split>.jsonl (one JSON row per line, the parquet rows verbatim) for
the three splits; when <split>.parquet is present too its sha256 is checked against the pinned
value. (The repository .venv has no pyarrow: convert the pinned parquet files with a separate
scratch environment, e.g. `pyarrow.parquet.read_table(p).to_pylist()`.)

Selection (seed 260904170, the math pool seed), deterministic:
  1. A base task is eligible when its prompt parses as imports + one function named
     entry_point with a docstring, its original test parses as one `def check(candidate)` with
     top-level asserts and no internal conflict, and every split carries the same prompt and
     original_test. A variant is allowed for a task when its mutated test conforms: oneoff
     differs from the original in exactly one assert (same assert count, one position);
     conflicting repeats a candidate input with a different expectation (an assert, or a
     `result = candidate(...)` statement with the asserts that follow it). Nonconforming
     variants are recorded in the private manifest and never assigned.
  2. 100 eligible base tasks are drawn with random.Random(seed).sample over sorted ids.
  3. Difficulty proxy (every upstream task is lcbhard): length of the prompt's docstring in
     characters, ties broken by numeric id. The 100 tasks are sorted by the proxy and cut into
     consecutive strata of 3; each full stratum gets the first permutation of
     (original, oneoff, conflicting), in a seeded random order, that only uses allowed
     variants; the leftover task is oneoff. Result: 34 oneoff, 33 conflicting, 33 original,
     with every variant spread evenly over the difficulty range.

Usage: python scripts/prepare_coding_dataset.py RAW_DIR [--repo .]
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
import random
import re
import shutil
import textwrap
from pathlib import Path

UPSTREAM_DATASET = 'fjzzq2002/impossible_livecodebench'
UPSTREAM_REVISION = '98650ffc3f28a01b261669b6d19fcd7773823710'
UPSTREAM_PAPER = 'https://arxiv.org/abs/2510.20270'
UPSTREAM_CODE = 'https://github.com/safety-research/impossiblebench'
UPSTREAM_CODE_REVISION = 'd4f9a149db2b9cec0fed7bab01d91c07ee10aca2'
LIVECODEBENCH_CODE = 'https://github.com/LiveCodeBench/LiveCodeBench'
PARQUET_SHA256 = {
    'original': '3f128b7a8419fd4f2d1c11f3eca7cd5bceac0e2385810950e926276d07a784c5',
    'oneoff': '31a89b06195a223268c4e66ea9ae87c5ed5b744bb7d230ed950d2e012c2c6c56',
    'conflicting': '9a13116c6cf48e8c2dc64ca2facdac4122aad30fe1aa52ab552a3b35638c22c8',
}
LICENSE_NOTE = (
    'ImpossibleBench code (safety-research/impossiblebench) is MIT licensed (Copyright (c) 2025 '
    'ImpossibleBench Team); LiveCodeBench, the source of the problems, is MIT licensed (Copyright '
    '(c) 2024 LiveCodeBench). The HF dataset card declares no license. See '
    'data/coding/UPSTREAM_LICENSE.'
)
SPLITS = ('original', 'oneoff', 'conflicting')
VARIANTS = ('original', 'oneoff', 'conflicting')
TASK_SET = 'ilcb100'
SEED = 260904170
POOL_SIZE = 100
SCHEMA_VERSION = 1
SELECTION_METHOD = (
    'Eligible base tasks (prompt and original tests parse with ast) sampled with '
    'random.Random(260904170).sample(sorted ids, 100). Difficulty proxy: prompt docstring length '
    'in characters (ties by numeric id). Sorted by the proxy and cut into strata of 3; each full '
    'stratum gets the first seeded-random permutation of (original, oneoff, conflicting) whose '
    'mutated tests conform (oneoff: exactly one assert differs; conflicting: a repeated input '
    'with a different expectation); the leftover task is oneoff. Each base task appears once, '
    'in one variant.'
)
FILES = ('problem.md', 'solution.py', 'tests.py')
_ID = re.compile(r'^lcbhard_(\d+)$')


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def row_hash(row: dict) -> str:
    return sha256_text(json.dumps(row, sort_keys=True, ensure_ascii=False))


def id_number(task_id: str) -> int:
    match = _ID.match(task_id)
    if not match:
        raise ValueError(f'Unexpected task id {task_id!r}')
    return int(match.group(1))


def load_raw(raw: Path) -> dict[str, dict[str, dict]]:
    out = {}
    for split in SPLITS:
        parquet = raw / f'{split}.parquet'
        if parquet.exists():
            digest = hashlib.sha256(parquet.read_bytes()).hexdigest()
            if digest != PARQUET_SHA256[split]:
                raise SystemExit(f'{parquet}: sha256 {digest} != pinned {PARQUET_SHA256[split]}')
        rows = {}
        for line in (raw / f'{split}.jsonl').read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row['task_id']] = row
        out[split] = rows
    return out


def parse_prompt(prompt: str, entry_point: str) -> tuple[str, str]:
    """(signature block incl. imports, dedented docstring) of the prompt's function."""
    module = ast.parse(prompt + '\n    pass\n')
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    others = [n for n in module.body if not isinstance(n, (ast.FunctionDef, ast.Import, ast.ImportFrom))]
    if len(functions) != 1 or others or functions[0].name != entry_point:
        raise ValueError('prompt is not imports + one function named entry_point')
    func = functions[0]
    doc = ast.get_docstring(func)
    if not doc:
        raise ValueError('prompt function has no docstring')
    lines = prompt.splitlines()
    doc_node = func.body[0]
    signature = '\n'.join(lines[: doc_node.lineno - 1]).rstrip()
    return signature, doc


def check_asserts(test: str) -> list[ast.Assert]:
    module = ast.parse(test)
    funcs = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    if len(module.body) != 1 or len(funcs) != 1 or funcs[0].name != 'check':
        raise ValueError('test is not a single def check(...)')
    args = funcs[0].args
    if [a.arg for a in args.args] != ['candidate']:
        raise ValueError('check does not take exactly (candidate)')
    asserts = [n for n in funcs[0].body if isinstance(n, ast.Assert)]
    if not asserts:
        raise ValueError('check has no top-level asserts')
    return asserts


def _candidate_calls(node: ast.AST) -> list[str]:
    return sorted(
        ast.dump(n) for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'candidate'
    )


def oneoff_difference(original: str, mutated: str) -> dict:
    a, b = check_asserts(original), check_asserts(mutated)
    if len(a) != len(b):
        raise ValueError(f'oneoff assert count {len(b)} != original {len(a)}')
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if ast.dump(x) != ast.dump(y)]
    if len(diffs) != 1:
        raise ValueError(f'oneoff differs in {len(diffs)} asserts')
    i = diffs[0]
    return {'assert_index': i, 'original': ast.unparse(a[i]), 'mutated': ast.unparse(b[i]),
            'same_candidate_call': _candidate_calls(a[i]) == _candidate_calls(b[i])}


def _expectations(test: str) -> list[tuple[tuple, str, str]]:
    """(candidate-call key, expectation dump, source) per top-level statement that calls candidate.

    An assert is its own expectation; an assignment such as `result = candidate(x)` takes the
    following statements up to the next candidate call as its expectation."""
    check_asserts(test)
    body = ast.parse(test).body[0].body
    out, i = [], 0
    while i < len(body):
        calls = tuple(_candidate_calls(body[i]))
        if not calls:
            i += 1
            continue
        group = [body[i]]
        if not isinstance(body[i], ast.Assert):
            j = i + 1
            while j < len(body) and not _candidate_calls(body[j]):
                group.append(body[j])
                j += 1
            i = j
        else:
            i += 1
        out.append((calls, '|'.join(ast.dump(n) for n in group),
                    '\n'.join(ast.unparse(n) for n in group)))
    return out


def find_conflict(test: str) -> dict | None:
    seen: dict[tuple, tuple[str, str]] = {}
    for key, expectation, source in _expectations(test):
        prior = seen.get(key)
        if prior is not None and prior[0] != expectation:
            return {'first': prior[1], 'second': source}
        seen.setdefault(key, (expectation, source))
    return None


def conflicting_pair(original: str, mutated: str) -> dict:
    pair = find_conflict(mutated)
    if pair is None:
        raise ValueError('conflicting test has no repeated candidate input with a different expectation')
    pair['added_asserts'] = len(check_asserts(mutated)) - len(check_asserts(original))
    return pair


def inspect_task(task_id: str, raw: dict) -> dict:
    """Conformance record for one base task.

    Raises ValueError when the base task itself is unusable (prompt, original tests). A variant
    whose mutated tests do not conform is recorded in `nonconforming` and never assigned."""
    orig = raw['original'][task_id]
    entry = orig['entry_point']
    for split in SPLITS:
        row = raw[split][task_id]
        if row['prompt'] != orig['prompt'] or row['entry_point'] != entry:
            raise ValueError(f'{split} prompt/entry point differs from original')
        if row['original_test'] != orig['original_test']:
            raise ValueError(f'{split} original_test differs')
        if row['impossible_type'] != split:
            raise ValueError(f'{split} impossible_type is {row["impossible_type"]!r}')
    if orig['test'] != orig['original_test']:
        raise ValueError('original split test != original_test')
    signature, doc = parse_prompt(orig['prompt'], entry)
    n_tests = len(check_asserts(orig['test']))
    if find_conflict(orig['test']) is not None:
        raise ValueError('original tests already contain a conflicting pair')
    record = {'signature': signature, 'docstring': doc, 'n_tests': n_tests,
              'allowed': ['original'], 'nonconforming': {}}
    for variant, checker in (('oneoff', oneoff_difference), ('conflicting', conflicting_pair)):
        try:
            record[variant] = checker(orig['original_test'], raw[variant][task_id]['test'])
            record['allowed'].append(variant)
        except (ValueError, SyntaxError) as exc:
            record['nonconforming'][variant] = str(exc)
    return record


def select(raw: dict, seed: int = SEED, size: int = POOL_SIZE):
    ids = sorted(raw['original'], key=id_number)
    records, excluded = {}, []
    for task_id in ids:
        if not all(task_id in raw[s] for s in SPLITS):
            excluded.append({'task_id': task_id, 'reason': 'missing from a split'})
            continue
        try:
            records[task_id] = inspect_task(task_id, raw)
        except (ValueError, SyntaxError) as exc:
            excluded.append({'task_id': task_id, 'reason': str(exc)})
    restricted = [{'task_id': t, 'nonconforming_variants': r['nonconforming']}
                  for t, r in records.items() if r['nonconforming']]
    eligible = sorted(records, key=id_number)
    if len(eligible) < size:
        raise SystemExit(f'only {len(eligible)} eligible tasks, need {size}')
    rng = random.Random(seed)
    chosen = rng.sample(eligible, size)
    not_sampled = sorted(set(eligible) - set(chosen), key=id_number)
    by_proxy = sorted(chosen, key=lambda t: (len(records[t]['docstring']), id_number(t)))
    variants = {}
    full = len(by_proxy) - len(by_proxy) % 3
    for start in range(0, full, 3):
        stratum = by_proxy[start:start + 3]
        perms = list(itertools.permutations(VARIANTS))
        rng.shuffle(perms)
        perm = next((p for p in perms
                     if all(v in records[t]['allowed'] for t, v in zip(stratum, p))), None)
        if perm is None:
            raise SystemExit(f'no conforming variant permutation for stratum {stratum}')
        variants.update(zip(stratum, perm))
    for task_id in by_proxy[full:]:
        variants[task_id] = next(v for v in ('oneoff', 'conflicting', 'original')
                                 if v in records[task_id]['allowed'])
    strata = {t: i // 3 for i, t in enumerate(by_proxy)}
    return records, variants, strata, excluded + restricted, not_sampled


def problem_markdown(task_id: str, entry: str, signature: str, doc: str) -> str:
    return (
        f'# {entry}\n\n'
        f'Task `{task_id}`. Implement the function `{entry}` in `solution.py`.\n\n'
        f'## Signature\n\n```python\n{signature}\n```\n\n'
        f'## Statement\n\n{doc.strip()}\n'
    )


def starter_source(prompt: str) -> str:
    return prompt.rstrip() + '\n    pass\n'


def write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return sha256_text(text)


def build(raw_dir: Path, repo: Path) -> dict:
    raw = load_raw(raw_dir)
    records, variants, strata, excluded, not_sampled = select(raw)
    public_root = repo / 'data' / 'coding'
    private_root = repo / 'private' / 'data' / 'coding'
    for stale in (public_root / 'tasks', private_root / 'reference'):
        if stale.exists():
            shutil.rmtree(stale)
    ordered = sorted(variants, key=id_number)
    public_tasks, private_tasks = [], []
    for task_id in ordered:
        rec, variant = records[task_id], variants[task_id]
        orig = raw['original'][task_id]
        entry = orig['entry_point']
        visible = raw[variant][task_id]['test']
        task_dir = public_root / 'tasks' / task_id
        hashes = {
            'problem.md': write_text(task_dir / 'problem.md',
                                     problem_markdown(task_id, entry, rec['signature'], rec['docstring'])),
            'solution.py': write_text(task_dir / 'solution.py', starter_source(orig['prompt'])),
            'tests.py': write_text(task_dir / 'tests.py', visible.rstrip() + '\n'),
        }
        ref_hash = write_text(private_root / 'reference' / task_id / 'original_test.py',
                              orig['original_test'].rstrip() + '\n')
        public_tasks.append({
            'task_id': task_id, 'title': entry, 'entry_point': entry,
            'dir': f'data/coding/tasks/{task_id}',
            'files': {name: {'path': f'data/coding/tasks/{task_id}/{name}', 'sha256': hashes[name]}
                      for name in FILES},
        })
        mutation = None
        if variant == 'oneoff':
            mutation = rec['oneoff']
        elif variant == 'conflicting':
            mutation = rec['conflicting']
        private_tasks.append({
            'task_id': task_id, 'variant': variant, 'entry_point': entry,
            'difficulty_proxy_docstring_chars': len(rec['docstring']),
            'difficulty_stratum': strata[task_id],
            'visible_test_count': len(check_asserts(visible)),
            'reference_test_count': rec['n_tests'],
            'mutation': mutation,
            'files': {
                'reference/original_test.py': {
                    'path': f'private/data/coding/reference/{task_id}/original_test.py', 'sha256': ref_hash},
            },
            'public_sha256': {name: hashes[name] for name in FILES},
            'upstream_row_sha256': {s: row_hash(raw[s][task_id]) for s in SPLITS},
            'upstream_visible_split': variant,
        })
    counts = {v: sum(1 for t in variants.values() if t == v) for v in VARIANTS}
    upstream = {
        'dataset': UPSTREAM_DATASET, 'revision': UPSTREAM_REVISION,
        'url': f'https://huggingface.co/datasets/{UPSTREAM_DATASET}',
        'paper': UPSTREAM_PAPER, 'code': UPSTREAM_CODE, 'code_revision': UPSTREAM_CODE_REVISION,
        'problems_source': LIVECODEBENCH_CODE, 'license': LICENSE_NOTE,
        'parquet_sha256': PARQUET_SHA256,
    }
    public = {
        'schema_version': SCHEMA_VERSION, 'task_set': TASK_SET, 'upstream': upstream,
        'selection_seed': SEED, 'selection_method': SELECTION_METHOD,
        'task_count': len(public_tasks), 'tasks': public_tasks,
    }
    public_text = json.dumps(public, indent=2, ensure_ascii=False) + '\n'
    write_text(public_root / 'manifest.json', public_text)
    private = {
        'schema_version': SCHEMA_VERSION, 'task_set': TASK_SET, 'upstream': upstream,
        'selection_seed': SEED, 'selection_method': SELECTION_METHOD,
        'public_manifest_sha256': sha256_text(public_text),
        'variant_counts': counts,
        'excluded_nonconforming': excluded,
        'eligible_not_sampled': not_sampled,
        'tasks': private_tasks,
    }
    write_text(private_root / 'manifest.private.json',
               json.dumps(private, indent=2, ensure_ascii=False) + '\n')
    return {'counts': counts, 'excluded': excluded, 'not_sampled': not_sampled}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('raw_dir', type=Path)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    summary = build(args.raw_dir.resolve(), args.repo.resolve())
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
