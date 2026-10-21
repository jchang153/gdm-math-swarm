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

DEFAULT_REPO = 'gdm-math-swarm/gdm-math-swarm-experiments'
ROOT_FILES = {'README.md', 'robots.txt', 'index.html', 'experiments.json'}
CATALOGUE = {'index.html', 'experiments.json', 'archive-manifest.json'}
# A key starts at a token boundary: an sk-/hf_ run inside a longer base64/base64url token (for example
# a provider's encrypted reasoning blob in an Inspect log or session snapshot) is not a key.
SECRET = re.compile(rb'(?<![A-Za-z0-9+/_-])(?:sk-[A-Za-z0-9_-]{24,}|hf_[A-Za-z0-9]{30,})'
                    rb'|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----')
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
    if path.suffix == '.eval':
        try:
            import zipfile_zstd  # noqa: F401; registers Inspect's ZIP method
        except ImportError as error:
            raise ValueError('Install the publication extra to screen Inspect archives') from error
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if SECRET.search(archive.read(name)):
                    raise ValueError('Credential-like content in Inspect archive; upload refused')


def load_bundle(bundle):
    bundle = Path(bundle)
    if bundle.is_symlink():
        raise ValueError('Linked bundle refused')
    for path in bundle.rglob('*'):
        if path.is_symlink():
            raise ValueError('Linked archive entry refused')
        if path.is_file():
            safe_path(path.relative_to(bundle).as_posix())
    manifest = json.loads((bundle / 'archive-manifest.json').read_text())
    if manifest.get('schema') != 'gdm-private-space/1':
        raise ValueError('Expected a prepare_hf_library.py bundle')
    runs = manifest['runs']
    if not runs or len(set(runs)) != len(runs) or any(not RUN_ID.fullmatch(x) for x in runs):
        raise ValueError('Invalid or duplicate run IDs')
    actual = {x.relative_to(bundle).as_posix() for x in bundle.rglob('*') if x.is_file()}
    if actual != set(manifest['files']) | {'archive-manifest.json'}:
        raise ValueError('Bundle contents differ from the manifest')
    for name, info in manifest['files'].items():
        safe_path(name)
        if '/' not in name and name not in ROOT_FILES:
            raise ValueError('Unexpected root file')
        if '/' in name and name.split('/')[0] not in runs:
            raise ValueError('File outside selected runs')
        path = bundle / name
        if digest(path.read_bytes()) != info:
            raise ValueError('Bundle checksum mismatch: ' + name)
        screen(path)
    catalogue = json.loads((bundle / 'experiments.json').read_text())
    if catalogue.get('warnings'):
        raise ValueError('Bundle catalogue has warnings')
    entries = catalogue['experiments']
    if {x['run'] for x in entries} != set(runs) or len(entries) != len(runs):
        raise ValueError('Bundle catalogue does not match selected runs')
    for run in runs:
        for required in ('results.json', 'results.html', 'transcripts/index.html',
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
            operation=None, unlisted=None):
    manifest, incoming = load_bundle(bundle)
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
    uploads = []
    for name in sorted(manifest['files']):
        if name in CATALOGUE:
            continue
        data = (bundle / name).read_bytes()
        if name in ROOT_FILES and name in remote:
            continue  # Preserve existing Space configuration.
        if name in remote:
            if not matches(data, remote[name]):
                raise ValueError('Immutable remote run file differs: ' + name)
        else:
            uploads.append((name, bundle / name))
    files = dict(old_manifest['files'])
    for name in manifest['files']:
        if name not in ROOT_FILES:
            files[name] = manifest['files'][name]
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
    root_uploads.append(('archive-manifest.json', json.dumps(merged_manifest, indent=2).encode()))
    root_uploads = [(n, d) for n, d in root_uploads if n not in remote or not matches(d, remote[n])]
    result = {'repo': repo, 'private': True, 'base_revision': info.sha,
              'runs': [x['run'] for x in entries], 'new_run_files': len(uploads),
              'catalogue_files': len(root_uploads), 'committed': False}
    if not commit:
        return result
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
    args = parser.parse_args()
    if args.receipt and args.receipt.resolve().is_relative_to(args.bundle.resolve()):
        parser.error('--receipt must be outside the immutable prepared bundle')
    from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download
    api = HfApi()
    print(json.dumps(publish(args.bundle, api, hf_hub_download, commit=args.commit,
          repo=args.repo_id, receipt=args.receipt, operation=CommitOperationAdd), indent=2))


if __name__ == '__main__':
    main()
