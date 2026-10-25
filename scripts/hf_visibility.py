"""The one record of the expected visibility of the HF repositories the publication tools write to.

docs/plan.md, "Public results Space, private logs dataset (October 24, 2026)": the owner made the results
Space ``gdm-math-swarm/gdm-math-swarm-experiments`` public themselves; the logs dataset
``HF-ORG/LOGS-DATASET`` (Inspect logs, raw records, snapshots, databases, transcripts,
timelines, resume bundles) stays private. Every tool that writes to the Space or the dataset (publication,
maintenance scopes, the live mirror, the dataset migration) compares the ACTUAL visibility with this record
before any write and again at every revision re-check, and refuses on any difference: a Space that turned
private again unexpectedly is refused as well as a dataset that turned public. A repository not recorded here
is expected to be private (fail closed).

These tools never change visibility: no code path calls ``update_repo_settings``/``update_repo_visibility``
or creates a repository that is not private (tests/test_hf_visibility.py). Only the owner changes the
visibility of a repository on huggingface.co; after such a change, update ``EXPECTED`` and docs/plan.md.
"""

SPACE_REPO = 'gdm-math-swarm/gdm-math-swarm-experiments'
DATASET_REPO = 'HF-ORG/LOGS-DATASET'

# (repo_type, repo) -> (expected private, why)
EXPECTED = {
    ('space', SPACE_REPO): (False, 'public by owner decision 2026-10-24 (docs/plan.md)'),
    ('dataset', DATASET_REPO): (True, 'private: Inspect logs and raw records (docs/plan.md, October 24, 2026)'),
}
UNRECORDED = (True, 'not recorded in scripts/hf_visibility.py, so expected private')


def label(private):
    return 'private' if private else 'public'


def expected_private(repo, repo_type):
    """True when ``repo`` of ``repo_type`` ('space' or 'dataset') is expected to be private."""
    return EXPECTED.get((repo_type, repo), UNRECORDED)[0]


def matches(info, repo, repo_type):
    """True when a repo_info result's visibility equals the recorded one (``private`` must be a bool)."""
    private = getattr(info, 'private', None)
    return isinstance(private, bool) and private == expected_private(repo, repo_type)


def check(info, repo, repo_type):
    """Raise unless the actual visibility of ``info`` (a repo_info result) is the recorded one; returns info."""
    if not matches(info, repo, repo_type):
        expected, why = EXPECTED.get((repo_type, repo), UNRECORDED)
        private = getattr(info, 'private', None)
        actual = label(private) if isinstance(private, bool) else 'of unknown visibility'
        raise ValueError(f'The HF {repo_type} {repo} is {actual}, but its recorded visibility is {label(expected)} '
                         f'({why}); refusing to write. If the owner changed its visibility, update '
                         'scripts/hf_visibility.py and docs/plan.md first.')
    return info


def check_dataset(api, repo=DATASET_REPO):
    """The logs dataset's repo_info after checking its visibility (pages of every Space write link to it)."""
    return check(api.repo_info(repo, repo_type='dataset'), repo, 'dataset')


def unchanged(info, repo, repo_type, revision):
    """Revision re-check: the visibility is still the recorded one and the head is still ``revision``."""
    return matches(info, repo, repo_type) and info.sha == revision


def record(repo, repo_type):
    """The visibility fields of a result or receipt (the check has passed, so actual equals expected)."""
    return {'visibility': label(expected_private(repo, repo_type)),
            'expected_visibility': label(expected_private(repo, repo_type))}
