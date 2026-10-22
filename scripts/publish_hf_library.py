#!/usr/bin/env python3
"""Publish a prepared run bundle to the shared private HF library.

Default: read-only planning. --commit uploads with optimistic revision guards.
Uses the publisher's own HF SDK login; never loads a model credential or starts
an experiment. Existing runs are merged into the chronological catalogue.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile

from build_experiment_index import newest_first, unlisted_runs, write_index
import dataset_logs
from dataset_logs import COMMIT_PLACEHOLDER, DATASET_REPO, DATASET_SCHEMA

DEFAULT_REPO = 'gdm-math-swarm/gdm-math-swarm-experiments'
ROOT_FILES = {'README.md', 'robots.txt', 'index.html', 'experiments.json', dataset_logs.SIGNIN_FILE}
# Root files that are Space configuration: an existing copy is preserved, never replaced.
CONFIG_FILES = {'README.md', 'robots.txt'}
DATASET_DIR = 'dataset-files'  # prepare_hf_library.py: files bound for the private logs dataset
CATALOGUE = {'index.html', 'experiments.json', 'archive-manifest.json'}
# A key starts at a token boundary: an sk-/hf_ run inside a longer base64/base64url token (for example
# a provider's encrypted reasoning blob in an Inspect log or session snapshot) is not a key.
_SECRET_PATTERN = re.compile(rb'(?<![A-Za-z0-9+/_-])(?:sk-[A-Za-z0-9_-]{24,}|hf_[A-Za-z0-9]{30,})'
                             rb'|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----')
# Inspect SWE's fixed, public placeholder key (in its open-source code; scripts/isolation_probes.py
# BRIDGE_PLACEHOLDER), written into every agent's apiKeyHelper setting. Only this exact token is
# exempt; a longer key that merely starts with it still matches in full and is refused.
# Owner, October 22, 2026: "I approve".
PUBLIC_PLACEHOLDERS = frozenset({b'sk-ant-api03-DOq5tyLPrk9M4hPE'})


class _SecretScreen:
    """``SECRET.search(data)``: the first credential-like match that is not a public placeholder."""
    def search(self, data):
        for match in _SECRET_PATTERN.finditer(data):
            if match.group(0) not in PUBLIC_PLACEHOLDERS:
                return match
        return None


SECRET = _SecretScreen()
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')


def safe_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '\\' in name or any(
            x in ('', '.', '..') or x.startswith('.') for x in name.split('/')):
        raise ValueError('Unsafe archive path')
    if path.name.lower() in {'host.env', 'credentials', 'credentials.json',
                              'token', 'token.json', 'stored_tokens', 'budget.sqlite',
                              'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa',
                              'application_default_credentials.json', 'credentials.db',
                              'access_tokens.db'} or path.suffix.lower() in {'.pem', '.key'}:
        raise ValueError('Host-only or credential filename in archive')
    return path


def digest(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def matches(data, remote):
    if remote.lfs:
        return remote.lfs.sha256 == hashlib.sha256(data).hexdigest()
    return remote.blob_id == hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def screen(path):
    # Check obvious credential filenames before reading contents. No match values
    # appear in diagnostics. Compressed Inspect archives need scanning too.
    safe_path(path.name)
    if SECRET.search(path.read_bytes()):
        raise ValueError('Credential-like content detected; upload refused')
    if path.name.endswith('.tar.gz') and 'resume' in path.parts:
        # a resume-bundle agent archive (research_swarm.resume_bundle), bound for the private dataset only:
        # every regular member is screened (its names include the agent's dot-folders by design)
        from prepare_hf_library import resume_archive_members
        for data in resume_archive_members(path.read_bytes()).values():
            if SECRET.search(data):
                raise ValueError('Credential-like content in resume archive; upload refused')
    elif path.name.endswith('.tar.gz'):  # a run's snapshot archive (prepare_hf_library.py): screen every member
        from prepare_hf_library import snapshot_archive_members
        for name, data in snapshot_archive_members(path.read_bytes()).items():
            safe_path(name)
            if SECRET.search(data):
                raise ValueError('Credential-like content in snapshot archive; upload refused')
    if path.suffix == '.eval':
        try:
            import zipfile_zstd  # noqa: F401; registers Inspect's ZIP method
        except ImportError as error:
            raise ValueError('Install the publication extra to screen Inspect archives') from error
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if SECRET.search(archive.read(name)):
                    raise ValueError('Credential-like content in Inspect archive; upload refused')


def load_dataset_files(bundle, runs):
    """The bundle's dataset-files/manifest.json files ({dataset path: record}), each checked against its
    digest, path rules and the credential screen; {} when the bundle has none."""
    root = Path(bundle) / DATASET_DIR
    actual = {x.relative_to(root).as_posix() for x in root.rglob('*') if x.is_file()} if root.is_dir() else set()
    if not actual:
        return {}
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('schema') != DATASET_SCHEMA or not isinstance(manifest.get('files'), dict):
        raise ValueError('Expected a prepare_hf_library.py dataset-files manifest')
    files = manifest['files']
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('Dataset files differ from their manifest')
    for name, info in files.items():
        safe_path(name)
        parts = name.split('/')
        if parts[0] not in runs or info.get('run') != parts[0] or dataset_logs.category('/'.join(parts[1:])) is None:
            raise ValueError('Dataset file outside the selected runs or categories: ' + name)
        for source in info.get('sources', []):
            if dataset_logs.dataset_path(parts[0], source) != name:
                raise ValueError('Dataset file source does not map to its path: ' + name)
        path = root / name
        if digest(path.read_bytes()) != {'bytes': info['bytes'], 'sha256': info['sha256']}:
            raise ValueError('Dataset file checksum mismatch: ' + name)
        screen(path)
    return files


def load_bundle(bundle):
    bundle = Path(bundle)
    if bundle.is_symlink():
        raise ValueError('Linked bundle refused')
    for path in bundle.rglob('*'):
        if path.is_symlink():
            raise ValueError('Linked archive entry refused')
        if path.is_file():
            relative = safe_path(path.relative_to(bundle).as_posix())
            if len(relative.parts) > 2 and relative.parts[0] != DATASET_DIR and relative.parts[1] == 'resume':
                # RUN/resume/ (research_swarm.resume_bundle) is private raw state: the private dataset only.
                raise ValueError('Resume bundle in the Space part of the archive; upload refused')
    manifest = json.loads((bundle / 'archive-manifest.json').read_text())
    if manifest.get('schema') != 'gdm-private-space/1':
        raise ValueError('Expected a prepare_hf_library.py bundle')
    runs = manifest['runs']
    if not runs or len(set(runs)) != len(runs) or any(not RUN_ID.fullmatch(x) for x in runs):
        raise ValueError('Invalid or duplicate run IDs')
    actual = {x.relative_to(bundle).as_posix() for x in bundle.rglob('*')
              if x.is_file() and x.relative_to(bundle).parts[0] != DATASET_DIR}
    if actual != set(manifest['files']) | {'archive-manifest.json'}:
        raise ValueError('Bundle contents differ from the manifest')
    manifest['dataset'] = load_dataset_files(bundle, runs)
    recorded = manifest.get('dataset_files', {})
    for name, record in recorded.items():
        run, _, rel = name.partition('/')
        info = manifest['dataset'].get(record.get('path'))
        if (run not in runs or info is None or record.get('repo') != DATASET_REPO
                or dataset_logs.dataset_path(run, rel) != record['path'] or rel not in info.get('sources', [])
                or (record.get('bytes'), record.get('sha256')) != (info['bytes'], info['sha256'])):
            raise ValueError('archive-manifest.json dataset_files record does not match the dataset files: ' + name)
    for name, info in manifest['files'].items():
        safe_path(name)
        if '/' in name and dataset_logs.category(name.split('/', 1)[1]) is not None \
                and manifest.get('layout') != 'records-in-space':
            raise ValueError('File bound for the logs dataset, not the Space: ' + name)
        if '/' not in name and name not in ROOT_FILES:
            raise ValueError('Unexpected root file')
        if '/' in name and name.split('/')[0] not in runs:
            raise ValueError('File outside selected runs')
        if '/' in name:
            from incident_site import incident_excluded
            if incident_excluded(name.split('/', 1)[1]):  # only report.html, report.md, evidence.json
                raise ValueError('Incident file outside the publication allowlist: ' + name)
        path = bundle / name
        if digest(path.read_bytes()) != info:
            raise ValueError('Bundle checksum mismatch: ' + name)
        screen(path)
    from prepare_hf_library import SNAPSHOT_ARCHIVE, SNAPSHOT_MANIFEST, SNAPSHOTS_DIR, check_snapshot_archive
    loose = sorted(n for n in actual if n.split('/')[1:2] == [SNAPSHOTS_DIR])
    if loose:
        raise ValueError(f'Bundle has {len(loose)} loose snapshot file(s) (for example {loose[0]}); prepare it again '
                         'with the current prepare_hf_library.py, which archives them per run')
    for run in runs:
        archived = [run + '/' + name in actual for name in (SNAPSHOT_ARCHIVE, SNAPSHOT_MANIFEST)]
        if any(archived) and not all(archived):
            raise ValueError(f'{run} needs both {SNAPSHOT_ARCHIVE} and {SNAPSHOT_MANIFEST}')
        if all(archived):
            check_snapshot_archive((bundle / run / SNAPSHOT_ARCHIVE).read_bytes(),
                                   (bundle / run / SNAPSHOT_MANIFEST).read_bytes(), run)
    catalogue = json.loads((bundle / 'experiments.json').read_text())
    if catalogue.get('warnings'):
        raise ValueError('Bundle catalogue has warnings')
    entries = catalogue['experiments']
    if {x['run'] for x in entries} != set(runs) or len(entries) != len(runs):
        raise ValueError('Bundle catalogue does not match selected runs')
    for run in runs:
        for required in ('results.json', 'results.html',
                         'conference-view/index.html', 'inspect-view/index.html',
                         'inspect-view/logs/listing.json'):
            if run + '/' + required not in actual:
                raise ValueError('Missing run export: ' + run + '/' + required)
    return manifest, entries


def merge_entries(existing, incoming):
    combined = {}
    for group in (existing, incoming):
        for entry in group:
            run = entry['run']
            if not RUN_ID.fullmatch(run):
                raise ValueError('Invalid catalogue run ID')
            stamp = datetime.datetime.fromisoformat(entry['start_utc'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Catalogue start time requires a timezone')
            for target in entry['links'].values():
                safe_path(target.rstrip('/'))
                if not target.startswith(run + '/'):
                    raise ValueError('Catalogue link leaves its run')
            if run in combined and entry != combined[run]:
                raise ValueError('Existing catalogue entry differs: ' + run)
            combined[run] = entry
    return newest_first(combined.values())


def payload(data):
    return data.read_bytes() if isinstance(data, Path) else data


def payload_size(data):
    return data.stat().st_size if isinstance(data, Path) else len(data)


def batches(items):
    batch, size = [], 0
    for name, data in items:
        if batch and (len(batch) >= 120 or size + payload_size(data) > 4_000_000):
            yield batch
            batch, size = [], 0
        batch.append((name, data))
        size += payload_size(data)
        if size > 4_000_000:
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def remote_files(api, repo, revision):
    # repo_info.siblings can truncate large inventories. The tree iterator
    # follows pagination; pin it to the same commit used for every comparison.
    return {item.path: item for item in api.list_repo_tree(
        repo, repo_type='space', revision=revision, recursive=True)
        if hasattr(item, 'blob_id')}


def validate_lfs_additions(old, new, selected, changed, final_files):
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if new_lines[:len(old_lines)] != old_lines:
        raise ValueError('Existing LFS attributes changed')
    for line in new_lines[len(old_lines):]:
        suffix = b' filter=lfs diff=lfs merge=lfs -text'
        if not line.endswith(suffix):
            raise ValueError('Unexpected LFS attributes addition')
        name = line[:-len(suffix)].decode('utf-8')
        safe_path(name)
        if (name.split('/')[0] not in selected or name not in changed
                or name not in final_files or not final_files[name].lfs):
            raise ValueError('LFS attributes addition is not a newly uploaded run file')


def publish(bundle, api, download, *, commit=False, repo=DEFAULT_REPO, receipt=None,
            operation=None, unlisted=None, dataset_repo=DATASET_REPO, records_in_space=False):
    """records_in_space: accept a legacy-layout bundle (prepare_hf_library.py --records-in-space); only for
    reproducing runs published before the logs dataset (no CLI flag: new runs put their logs and raw records
    in the dataset)."""
    manifest, incoming = load_bundle(bundle)
    if manifest.get('layout') == 'records-in-space' and not records_in_space:
        raise ValueError('Legacy records-in-space bundle refused: Inspect logs and raw records go to the logs '
                         'dataset (prepare without --records-in-space)')
    dataset_files = manifest.pop('dataset')
    if dataset_files and dataset_repo != DATASET_REPO:
        raise ValueError('The prepared bundle records the dataset ' + DATASET_REPO)
    unlisted = set(unlisted_runs() if unlisted is None else unlisted)
    hidden = sorted(set(manifest['runs']) & unlisted)
    if hidden:
        raise ValueError('Run(s) unlisted by data/library-unlisted.json; remove the record to publish them: '
                         + ', '.join(hidden))
    bundle = Path(bundle)
    info = api.repo_info(repo, repo_type='space', files_metadata=True)
    if not info.private:
        raise ValueError('Publication requires a private Space')
    remote = remote_files(api, repo, info.sha)
    def read_json(name, empty):
        if name not in remote:
            return empty
        data = Path(download(repo, name, repo_type='space', revision=info.sha)).read_bytes()
        if not matches(data, remote[name]):
            raise ValueError('Remote metadata mismatch: ' + name)
        return json.loads(data)
    old_index = read_json('experiments.json', {'experiments': [], 'warnings': []})
    old_manifest = read_json('archive-manifest.json', {'files': {}, 'runs': []})
    if old_index.get('warnings'):
        raise ValueError('Existing catalogue requires review')
    # Unlisted runs keep their files but never return to the catalogue (docs/plan.md, October 20, 2026).
    entries = [x for x in merge_entries(old_index['experiments'], incoming) if x['run'] not in unlisted]
    # Never remove an existing run or alter an existing run file. Matching partial
    # uploads can resume after a failed batch; conflicting IDs fail closed.
    selected = set(manifest['runs'])
    for name in remote:
        if name.split('/')[0] in selected and name not in manifest['files']:
            raise ValueError('Remote run contains an unstaged file: ' + name)
    # The dataset goes first (no commit spans both repositories); its commit is then pinned into every
    # Space page that carries the placeholder (Inspect View shims, repointed record links).
    dataset_result = None
    pinned = {}
    if dataset_files:
        dataset_info = api.repo_info(dataset_repo, repo_type='dataset')
        if not dataset_info.private:
            raise ValueError('The logs dataset must be private')
        dataset_result = {'repo': dataset_repo, 'files': len(dataset_files),
                          'bytes': sum(x['bytes'] for x in dataset_files.values()),
                          'base_revision': dataset_info.sha, 'committed': False}
        if commit:
            done = dataset_logs.commit_files(
                api, {n: bundle / DATASET_DIR / n for n in sorted(dataset_files)}, operation=operation,
                repo=dataset_repo, message='Publish run files kept out of the Space: ' + ', '.join(manifest['runs']))
            for name, record in done['records'].items():
                if record != {'bytes': dataset_files[name]['bytes'], 'sha256': dataset_files[name]['sha256']}:
                    raise ValueError('Dataset copy differs from the prepared file: ' + name)
            dataset_result.update(committed=True, revision=done['revision'], added=len(done['added']),
                                  already_present=len(done['already_present']), verified=done['verified'])
            pin = done['revision']
        else:
            pin = None
        for name in manifest['files']:
            data = (bundle / name).read_bytes()
            if COMMIT_PLACEHOLDER.encode() in data:
                pinned[name] = dataset_logs.pin_commit(data, pin) if pin else data
    uploads = []
    space_digests = {}
    for name in sorted(manifest['files']):
        if name in CATALOGUE:
            continue
        data = pinned.get(name)
        if data is None:
            data = (bundle / name).read_bytes()
        space_digests[name] = digest(data)
        if name in CONFIG_FILES and name in remote:
            continue  # Preserve existing Space configuration.
        if name in ROOT_FILES and name in remote:
            if not matches(data, remote[name]):
                uploads.append((name, data))  # the site's own root page (sign-in) follows the bundle
            continue
        if name in remote:
            if not matches(data, remote[name]):
                raise ValueError('Immutable remote run file differs: ' + name)
        else:
            uploads.append((name, data if name in pinned else bundle / name))
    files = dict(old_manifest['files'])
    for name in manifest['files']:
        if name not in ROOT_FILES or name == dataset_logs.SIGNIN_FILE:
            files[name] = space_digests.get(name, manifest['files'][name])
    for name, data in uploads:
        files[name] = digest(payload(data))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        write_index(tmp, entries, hosted=True)
        root_uploads = [(name, (tmp / name).read_bytes()) for name in ('index.html', 'experiments.json')]
    for name, data in root_uploads:
        files[name] = digest(data)
    listed = [x['run'] for x in entries]
    merged_manifest = {**old_manifest, 'schema': 'gdm-private-space/1',  # unlisted runs stay recorded
                       'runs': listed + [x for x in old_manifest['runs'] if x not in listed], 'files': files,
                       'total_bytes': sum(x['bytes'] for x in files.values())}
    if manifest.get('dataset_files'):
        merged_manifest['dataset_repo'] = dataset_repo
        merged_manifest['dataset_files'] = {**old_manifest.get('dataset_files', {}), **{
            n: {**r, **({'commit': dataset_result['revision']} if dataset_result.get('revision') else {})}
            for n, r in manifest['dataset_files'].items()}}
    root_uploads.append(('archive-manifest.json', json.dumps(merged_manifest, indent=2).encode()))
    root_uploads = [(n, d) for n, d in root_uploads if n not in remote or not matches(d, remote[n])]
    result = {'repo': repo, 'private': True, 'base_revision': info.sha,
              'runs': [x['run'] for x in entries], 'new_run_files': len(uploads),
              'catalogue_files': len(root_uploads), 'committed': False,
              **({'dataset': dataset_result} if dataset_result else {})}
    if not commit:
        return result
    if receipt and dataset_result:
        Path(receipt).write_text(json.dumps({**result, 'status': 'dataset verified; Space upload pending'}, indent=2))
    unpinned = [n for n, d in uploads if COMMIT_PLACEHOLDER.encode() in payload(d)]
    if unpinned:
        raise ValueError('Page(s) still carry the dataset commit placeholder (no dataset files to pin): '
                         + ', '.join(unpinned[:5]))
    revision = info.sha
    for batch in [*batches(uploads), *([root_uploads] if root_uploads else [])]:
        current = api.repo_info(repo, repo_type='space')
        if not current.private or current.sha != revision:
            raise ValueError('Space changed concurrently; rerun to merge the latest catalogue')
        response = api.create_commit(repo_id=repo, repo_type='space', parent_commit=revision,
            commit_message='Publish shared experiment library: ' + ', '.join(manifest['runs']),
            operations=[operation(path_in_repo=n, path_or_fileobj=str(d) if isinstance(d, Path) else d) for n, d in batch])
        revision = response.oid
        if receipt:
            Path(receipt).write_text(json.dumps({**result, 'last_revision': revision,
                'status': 'uploading; catalogue may be pending'}, indent=2))
    final = api.repo_info(repo, repo_type='space', files_metadata=True)
    if not final.private or final.sha != revision:
        raise ValueError('Space changed before verification; inspect the latest revision')
    final_files = remote_files(api, repo, final.sha)
    changed = dict(uploads + root_uploads)
    if set(final_files) != set(remote) | set(changed):
        raise ValueError('Remote file inventory changed unexpectedly')
    for name, old in remote.items():
        if name not in changed:
            new = final_files[name]
            if old.blob_id != new.blob_id or old.lfs != new.lfs:
                if name != '.gitattributes':
                    raise ValueError('Unrelated remote file changed: ' + name)
                old_attrs = Path(download(repo, name, repo_type='space', revision=info.sha)).read_bytes()
                new_attrs = Path(download(repo, name, repo_type='space', revision=final.sha)).read_bytes()
                validate_lfs_additions(old_attrs, new_attrs, selected, changed, final_files)
    for name, data in changed.items():
        if not matches(payload(data), final_files[name]):
            raise ValueError('Uploaded file hash mismatch: ' + name)
    result.update(committed=True, revision=revision, uploaded_files=len(changed),
                  verified_preserved_files=len(remote) - len(set(remote) & set(changed)))
    if receipt:
        Path(receipt).write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--repo-id', default=DEFAULT_REPO)
    parser.add_argument('--commit', action='store_true', help='Upload; otherwise only plan')
    parser.add_argument('--receipt', type=Path, help='Host-only receipt outside the prepared bundle')
    parser.add_argument('--dataset-repo', default=DATASET_REPO,
                        help='private dataset for Inspect logs and raw records (default %(default)s)')
    args = parser.parse_args()
    if args.receipt and args.receipt.resolve().is_relative_to(args.bundle.resolve()):
        parser.error('--receipt must be outside the immutable prepared bundle')
    from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download
    api = HfApi()
    print(json.dumps(publish(args.bundle, api, hf_hub_download, commit=args.commit,
          repo=args.repo_id, receipt=args.receipt, operation=CommitOperationAdd,
          dataset_repo=args.dataset_repo), indent=2))


if __name__ == '__main__':
    main()
