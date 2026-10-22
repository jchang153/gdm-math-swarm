"""``maintain_hf_run.py --scope migrate-logs``: move published run files from the Space to the private logs
dataset (docs/experiment-library.md, "Logs and raw records in the dataset repo").

Stages, each recorded in the receipt and the backup directory:

1. **Plan** (read-only; the default). Pin the Space head; list every file of the selected published runs
   whose ``dataset_logs.category`` is selected (``live/`` is never touched). Download each at that revision,
   check it against the Space's own record (LFS SHA-256 or Git blob id) and the credential screen, and
   compare it with the local ``runs/RUN/`` copy when one is readable (iCloud placeholders are downloaded
   with ``brctl download`` first; one that stays a placeholder is recorded as unavailable). Write the
   before-inventory.
2. **Copy** (``--commit``). Guarded, verified dataset commits pinned to the dataset head add each file at
   ``dataset_logs.dataset_path`` (the two Space copies of an Inspect log become one ``RUN/inspect/<name>``).
   Every dataset path is verified (size and LFS SHA-256, or the Git blob id of a regular file); each Inspect
   log is also read through resolve (first and last 64 bytes, HTTP 206). Nothing in the Space changes.
3. **Delete** (``--commit --delete-moved``; download-only categories only, never ``eval``; the Inspect logs
   are switched and deleted by ``migrate_dataset.py switch`` and ``delete-eval``, see ``switch`` and
   ``delete_eval``). After stage 2 is verified, one
   Space commit pinned to the planned head (or to a later head that differs only under ``live/``) deletes
   the moved files, repoints every page link to them to the dataset file page and moves their
   ``archive-manifest.json`` records from ``files`` to ``dataset_files``. The result is verified, then the
   LFS objects the new head no longer references are permanently deleted without rewriting history
   (as ``--scope prune`` does). Originals of every changed page are backed up first.
"""
import datetime
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import dataset_logs
from dataset_logs import CATEGORIES, DATASET_REPO, category, dataset_path, digest, write_json
from publish_hf_library import DEFAULT_REPO, matches, remote_files, screen

DATALESS = getattr(stat, 'SF_DATALESS', 0x40000000)
MANIFEST = 'archive-manifest.json'
ATTRIBUTES = '.gitattributes'
PAGE_BATCH = 200


def _dataless(path):
    try:
        return bool(os.stat(path).st_flags & DATALESS)
    except (AttributeError, OSError):
        return False


_ICLOUD = {'failures': 0}
ICLOUD_ATTEMPTS = 2  # after this many placeholders that would not download, the rest are not waited for


def local_copy(path, runner=subprocess.run, timeout=90):
    """(bytes or None, note) of a local original, forcing an iCloud placeholder's download first."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None, 'absent'
    if _dataless(path) and _ICLOUD['failures'] >= ICLOUD_ATTEMPTS:
        return None, 'iCloud placeholder (not downloaded; earlier downloads did not complete)'
    if _dataless(path):
        try:
            runner(['brctl', 'download', str(path)], check=False, timeout=timeout, capture_output=True)
        except (OSError, subprocess.TimeoutExpired):
            pass
        import time
        deadline = time.monotonic() + timeout  # brctl only requests the download; wait for it
        while _dataless(path) and time.monotonic() < deadline:
            time.sleep(2)
        if _dataless(path):
            _ICLOUD['failures'] += 1
            return None, 'iCloud placeholder (download did not complete)'
    return path.read_bytes(), 'read'


def published_runs(root_manifest):
    runs = list(root_manifest.get('runs', []))
    return [r for r in runs if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', r)]


def plan(api, download, *, runs, categories, runs_dir, repo=DEFAULT_REPO, dataset=DATASET_REPO,
         runner=subprocess.run, cache_dir=None):
    """Stage 1: the read-only plan (see the module docstring)."""
    unknown = sorted(set(categories) - set(CATEGORIES))
    if not categories or unknown:
        raise ValueError('Unknown categories: ' + ', '.join(unknown or ['(none)']))
    info = api.repo_info(repo, repo_type='space', files_metadata=True)
    if not info.private:
        raise ValueError('Maintenance requires a private Space')
    base = info.sha
    remote = remote_files(api, repo, base)
    root = json.loads(Path(download(repo, MANIFEST, repo_type='space', revision=base,
                                    cache_dir=cache_dir)).read_bytes())
    known = set(published_runs(root))
    selected_runs = sorted(known if runs is None else runs)
    missing = sorted(set(selected_runs) - known)
    if missing:
        raise ValueError('Not published runs: ' + ', '.join(missing))
    files, targets = [], {}
    for name in sorted(remote):
        run, _, rel = name.partition('/')
        if run not in selected_runs or not rel:
            continue
        kind = category(rel)
        if kind not in categories:
            continue
        path = Path(download(repo, name, repo_type='space', revision=base, cache_dir=cache_dir))
        data = path.read_bytes()
        if not matches(data, remote[name]):
            raise ValueError('Downloaded Space copy differs from its record: ' + name)
        screen(path)  # credential screen (public placeholder exempt), Inspect and tar members included
        local, note = local_copy(Path(runs_dir) / run / rel, runner)
        if local is not None and local != data:
            local_check = 'differs'
        else:
            local_check = 'match' if local is not None else note
        target = dataset_path(run, rel)
        record = {'space_path': name, 'run': run, 'category': kind, 'dataset_path': target, **digest(data),
                  'lfs': remote[name].lfs is not None, 'local': local_check}
        if target in targets and targets[target]['sha256'] != record['sha256']:
            raise ValueError('Two Space copies map to one dataset path with different bytes: ' + target)
        targets.setdefault(target, {**digest(data), 'source': path, 'space_paths': []})
        targets[target]['space_paths'].append(name)
        files.append(record)
    # Files pruned earlier (archive-manifest.json offline_files) go to the dataset from their local originals
    # when those are readable with the recorded digest; pages linking the others get a note link instead.
    offline_unavailable = []
    for name, recorded in sorted(root.get('offline_files', {}).items()):
        run, _, rel = name.partition('/')
        if run not in selected_runs or category(rel) not in categories or name in remote:
            continue
        local, note = local_copy(Path(runs_dir) / run / rel, runner)
        if local is None or digest(local) != {'bytes': recorded['bytes'], 'sha256': recorded['sha256']}:
            offline_unavailable.append({'path': name, 'local': note if local is None else 'differs from the record'})
            continue
        target = dataset_path(run, rel)
        files.append({'space_path': None, 'offline_path': name, 'run': run, 'category': category(rel),
                      'dataset_path': target, **digest(local), 'lfs': False, 'local': 'match (offline original)'})
        targets[target] = {**digest(local), 'source': Path(runs_dir) / run / rel, 'space_paths': []}
    differing = [f['space_path'] for f in files if f['local'] == 'differs']
    if differing:
        raise ValueError('Local originals differ from the Space copies; nothing is copied: ' + ', '.join(differing[:20]))
    by_category = {}
    for f in files:
        if f['space_path'] is None:
            continue
        row = by_category.setdefault(f['category'], {'space_files': 0, 'space_bytes': 0, 'lfs_bytes': 0})
        row['space_files'] += 1
        row['space_bytes'] += f['bytes']
        row['lfs_bytes'] += f['bytes'] if f['lfs'] else 0
    return {'operation': 'maintain-run', 'scope': 'migrate-logs', 'repo': repo, 'dataset': dataset,
            'private': True, 'space_revision': base, 'used_storage_before': getattr(info, 'used_storage', None),
            'runs': selected_runs, 'categories': sorted(categories), 'files': files,
            'dataset_files': {t: {'bytes': r['bytes'], 'sha256': r['sha256'], 'space_paths': r['space_paths']}
                              for t, r in sorted(targets.items())},
            'unique_bytes': sum(r['bytes'] for r in targets.values()), 'by_category': by_category,
            'local_checks': {k: sum(1 for f in files if f['local'] == k) for k in {f['local'] for f in files}},
            'offline_copied': [f['offline_path'] for f in files if f['space_path'] is None],
            'offline_unavailable': offline_unavailable,
            '_sources': {t: r['source'] for t, r in targets.items()}, '_remote': remote, '_root': root}


def public(result):
    return {k: v for k, v in result.items() if not k.startswith('_')}


def copy(api, result, *, operation, session, headers, dataset=DATASET_REPO, progress=None):
    """Stage 2: dataset commits (one per run, in order) and per-file verification, plus ranged reads of
    every Inspect log through resolve."""
    sources = result['_sources']
    revision = None
    added, present = [], []
    for run in result['runs']:
        mine = {t: sources[t] for t in sources if t.split('/', 1)[0] == run}
        if not mine:
            continue
        outcome = dataset_logs.commit_files(
            api, mine, operation=operation, repo=dataset, progress=progress,
            message=f'Add {run} run files from the Space ({", ".join(result["categories"])})',
            max_bytes=400_000_000)
        revision = outcome['revision']
        added += outcome['added']
        present += outcome['already_present']
        for name, record in outcome['records'].items():
            expected = result['dataset_files'][name]
            if (record['bytes'], record['sha256']) != (expected['bytes'], expected['sha256']):
                raise ValueError('Dataset copy differs from the Space copy: ' + name)
    if revision is None:
        revision = api.repo_info(dataset, repo_type='dataset').sha
    final = dataset_logs.dataset_inventory(api, dataset, revision)
    checks = {}
    for name, record in result['dataset_files'].items():
        entry = final.get(name)
        ok = entry is not None and entry[0] == record['bytes'] and (entry[1] is None or entry[1] == record['sha256'])
        if entry is not None and entry[1] is None:  # a regular Git file: compare the blob id
            data = Path(sources[name]).read_bytes()
            ok = ok and entry[2] == dataset_logs._git_sha1(data)
        check = {'size_sha256': ok}
        if name.endswith('.eval'):
            data = Path(sources[name]).read_bytes()
            check['range'] = dataset_logs.check_ranges(session, headers, dataset, revision, name, data[:64],
                                                       data[-64:], len(data))
            ok = ok and check['range']['ok']
        check['ok'] = ok
        checks[name] = check
    failed = sorted(n for n, c in checks.items() if not c['ok'])
    if failed:
        raise ValueError('Dataset verification failed: ' + ', '.join(failed[:20]))
    return {'dataset_revision': revision, 'added': len(added), 'already_present': len(present),
            'verified': len(checks), 'range_checked': sum(1 for n in checks if n.endswith('.eval')),
            'checks': checks}


def delete(api, download, result, copied, *, operation, delete_operation, backup_dir, repo=DEFAULT_REPO,
           dataset=DATASET_REPO, purge=True, cache_dir=None, now=None, retries=3, pages_dir=None):
    """Stage 3 for download-only categories (see the module docstring)."""
    if 'eval' in result['categories']:
        raise ValueError('Deleting the Inspect logs from the Space needs the owner\'s separate approval; '
                         'run the delete stage without the eval category')
    base = result['space_revision']
    remote = result['_remote']
    moved = {f['space_path']: f for f in result['files'] if f['space_path'] is not None}
    offline = {f['offline_path']: f for f in result['files'] if f['space_path'] is None}
    unavailable = {u['path'] for u in result.get('offline_unavailable', [])}
    if not moved and not offline and not unavailable:
        return {'deleted': 0, 'already_applied': True}
    commit = copied['dataset_revision']
    removed = (now or datetime.datetime.now(datetime.timezone.utc)).date().isoformat()
    moved_by_run, kept_offline = {}, {}
    for name in [*moved, *offline]:
        run, _, rel = name.partition('/')
        moved_by_run.setdefault(run, set()).add(rel)
    for name in unavailable:
        run, _, rel = name.partition('/')
        kept_offline.setdefault(run, set()).add(rel)
    # Pages of these runs that link a moved file get the link repointed to the dataset file page; a link to a
    # file kept only offline points to the run page's "Raw records kept offline" notice.
    pages = {}
    for name in sorted(remote):
        run, _, rel = name.partition('/')
        if (run not in moved_by_run and run not in kept_offline) or not rel.endswith('.html') or name in moved:
            continue
        targets = moved_by_run.get(run, set()) | kept_offline.get(run, set())
        data = None
        if pages_dir is not None and (Path(pages_dir) / name).is_file():  # an earlier local copy, if identical
            data = (Path(pages_dir) / name).read_bytes()
            if not matches(data, remote[name]):
                data = None
        if data is None:
            data = Path(download(repo, name, repo_type='space', revision=base, cache_dir=cache_dir)).read_bytes()
        if not matches(data, remote[name]):
            raise ValueError('Remote page differs from its record: ' + name)
        text = data.decode('utf-8', 'replace')
        if not dataset_logs.moved_links(text, rel, targets):
            continue
        new, changed = dataset_logs.repoint_links(text, rel, run, moved_by_run.get(run, set()), commit, dataset)
        new, noted = dataset_logs.offline_links(new, rel, kept_offline.get(run, set()))
        if changed or noted:
            pages[name] = (data, new.encode('utf-8'), sorted(set(changed + noted)))
    root = result['_root']
    files = {n: d for n, d in root['files'].items() if n not in moved}
    for name, (_, new, _) in pages.items():
        files[name] = digest(new)
    dataset_files = dict(root.get('dataset_files', {}))
    for name, f in [*moved.items(), *offline.items()]:
        dataset_files[name] = {'repo': dataset, 'path': f['dataset_path'], 'commit': commit, 'bytes': f['bytes'],
                               'sha256': f['sha256'], 'category': f['category'], 'moved': removed,
                               **({'source': 'local original (pruned from the Space earlier)'}
                                  if name in offline else {})}
    new_root = {**root, 'files': files, 'total_bytes': sum(x['bytes'] for x in files.values()),
                'dataset_repo': dataset, 'dataset_files': dataset_files}
    uploads = {name: new for name, (_, new, _) in pages.items()}
    uploads[MANIFEST] = json.dumps(new_root, indent=2).encode()
    for name, data in uploads.items():
        if len(data) >= 9.5 * 1024 * 1024 and name != MANIFEST:
            raise ValueError('A repointed page would become LFS: ' + name)
    backup_dir = Path(backup_dir)
    for name, (old, _, _) in pages.items():
        target = backup_dir / 'original' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old)
    original_manifest = Path(download(repo, MANIFEST, repo_type='space', revision=base, cache_dir=cache_dir))
    (backup_dir / 'original').mkdir(parents=True, exist_ok=True)
    (backup_dir / 'original' / MANIFEST).write_bytes(original_manifest.read_bytes())
    write_json(backup_dir / 'deleted.json', [{k: v for k, v in f.items()} for f in moved.values()])
    write_json(backup_dir / 'repointed.json', {n: c for n, (_, _, c) in pages.items()})
    # The live mirror may commit under live/ meanwhile: rebase onto a head that differs only there.
    parent = base
    for attempt in range(retries + 1):
        latest = api.repo_info(repo, repo_type='space')
        if not latest.private:
            raise ValueError('The Space is no longer private')
        if latest.sha != parent:
            newer = remote_files(api, repo, latest.sha)
            changed = sorted(n for n in set(newer) | set(remote) if not n.startswith('live/') and (
                n not in newer or n not in remote or (newer[n].blob_id, newer[n].lfs) != (remote[n].blob_id, remote[n].lfs)))
            if changed:
                raise ValueError('The Space changed outside live/ since the plan; plan again: ' + ', '.join(changed[:20]))
            parent = latest.sha
        try:
            response = api.create_commit(
                repo_id=repo, repo_type='space', parent_commit=parent,
                commit_message=(f'Move {len(moved)} download-only run file(s) to the private logs dataset '
                                f'{dataset}@{commit[:8]} ({", ".join(result["categories"])}); repoint '
                                f'{len(pages)} page(s)'),
                operations=[*(delete_operation(path_in_repo=n) for n in sorted(moved)),
                            *(operation(path_in_repo=n, path_or_fileobj=d) for n, d in uploads.items())])
            break
        except Exception as error:  # a concurrent live/ commit: retry on the new head
            if attempt == retries or 'parent' not in str(error).lower() and '412' not in str(error):
                raise
    revision = response.oid
    final = remote_files(api, repo, revision)
    before = remote_files(api, repo, parent) if parent != base else remote
    expected = set(before) - set(moved)
    if set(final) != expected:
        raise ValueError(f'Space inventory differs after the commit: missing {sorted(expected - set(final))[:20]}, '
                         f'unexpected {sorted(set(final) - expected)[:20]}')
    for name in expected:
        if name in uploads:
            if not matches(uploads[name], final[name]):
                raise ValueError('Uploaded file mismatch: ' + name)
        elif (final[name].blob_id, final[name].lfs) != (before[name].blob_id, before[name].lfs):
            raise ValueError('Unrelated Space file changed: ' + name)
    outcome = {'space_revision': revision, 'parent': parent, 'deleted': len(moved), 'repointed_pages': sorted(pages),
               'offline_copied': sorted(offline), 'offline_linked_to_notice': sorted(unavailable),
               'deleted_bytes': sum(f['bytes'] for f in moved.values())}
    if purge:
        referenced = {item.lfs.sha256 for item in api.list_repo_tree(repo, repo_type='space', revision=revision,
                                                                       recursive=True)
                      if getattr(item, 'lfs', None) is not None}
        wanted = {f['sha256'] for f in moved.values() if f['lfs']} - referenced
        prefixes = tuple(run + '/' for run in moved_by_run)
        objects = [f for f in api.list_lfs_files(repo, repo_type='space')
                   if (f.file_oid in wanted) and str(f.filename).startswith(prefixes)]
        if objects:
            api.permanently_delete_lfs_files(repo, objects, rewrite_history=False, repo_type='space')
        outcome.update(purged_lfs_objects=len(objects), purged_bytes=sum(f.size for f in objects))
    return outcome


def migrate(api, download, *, runs=None, categories=('eval',), runs_dir, commit=False, delete_moved=False,
            receipt=None, backup_dir=None, operation=None, delete_operation=None, session=None, headers=None,
            repo=DEFAULT_REPO, dataset=DATASET_REPO, purge=True, runner=subprocess.run, cache_dir=None,
            pages_dir=None):
    if delete_moved and not commit:
        raise ValueError('--delete-moved requires --commit')
    if delete_moved and 'eval' in categories:
        raise ValueError('The delete stage never removes Inspect logs (owner approval pending); drop eval')
    if commit and (receipt is None or backup_dir is None or operation is None):
        raise ValueError('Committing requires --receipt, --backup-dir and an operation factory')
    result = plan(api, download, runs=runs, categories=categories, runs_dir=runs_dir, repo=repo, dataset=dataset,
                  runner=runner, cache_dir=cache_dir)
    if not commit:
        return public(result)
    receipt, backup_dir = Path(receipt), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    state = {**public(result), 'status': 'planned; nothing copied'}

    def record(status, **extra):
        state.update(extra, status=status)
        write_json(receipt, state)
    write_json(backup_dir / 'before-inventory.json', {
        'space_revision': result['space_revision'],
        'files': {n: {'blob_id': i.blob_id, 'lfs_sha256': i.lfs.sha256 if i.lfs else None, 'size': i.size}
                  for n, i in sorted(result['_remote'].items())}})
    write_json(backup_dir / 'plan.json', public(result))
    record('planned; copying to the dataset')
    try:
        copied = copy(api, result, operation=operation, session=session, headers=headers, dataset=dataset,
                      progress=lambda rev, group: record(f'copying; dataset at {rev}'))
        record('copied and verified; Space unchanged', copy={k: v for k, v in copied.items() if k != 'checks'},
               checks=copied['checks'])
        if delete_moved:
            record('copied and verified; deleting from the Space')
            deleted = delete(api, download, result, copied, operation=operation, delete_operation=delete_operation,
                             backup_dir=backup_dir, repo=repo, dataset=dataset, purge=purge, cache_dir=cache_dir,
                             pages_dir=pages_dir)
            info = api.repo_info(repo, repo_type='space')
            record('verified', delete=deleted, used_storage_after=getattr(info, 'used_storage', None))
        else:
            record('verified')
    except Exception as error:
        try:
            record('failed: ' + type(error).__name__ + ': ' + str(error)[:500])
        except Exception:
            print('Could not record the failure in the receipt', file=sys.stderr)
        raise
    return state


# --- stage 3 (switch) and stage 4 (delete-eval) of the Inspect logs ------------------------------------------------
SWITCH_KEY = 'dataset_switch'


def _commit_guarded(api, repo, base, remote, operations, message, retries=3):
    """One Space commit pinned to ``base``, rebased only onto a later head that differs from ``remote`` under
    ``live/`` alone (the live mirror may commit meanwhile). Returns (revision, parent)."""
    parent = base
    for attempt in range(retries + 1):
        latest = api.repo_info(repo, repo_type='space')
        if not latest.private:
            raise ValueError('The Space is no longer private')
        if latest.sha != parent:
            newer = remote_files(api, repo, latest.sha)
            changed = sorted(n for n in set(newer) | set(remote) if not n.startswith('live/') and (
                n not in newer or n not in remote
                or (newer[n].blob_id, newer[n].lfs) != (remote[n].blob_id, remote[n].lfs)))
            if changed:
                raise ValueError('The Space changed outside live/ since the plan; plan again: ' + ', '.join(changed[:20]))
            parent = latest.sha
        try:
            response = api.create_commit(repo_id=repo, repo_type='space', parent_commit=parent,
                                         commit_message=message, operations=operations)
            return response.oid, parent
        except Exception as error:  # a concurrent live/ commit: retry on the new head
            if attempt == retries or 'parent' not in str(error).lower() and '412' not in str(error):
                raise


def _verify_commit(api, repo, revision, parent, base, remote, uploads, deleted):
    """The Space at ``revision`` holds exactly the parent's files minus ``deleted`` plus ``uploads`` (bytes),
    every other file unchanged."""
    final = remote_files(api, repo, revision)
    before = remote_files(api, repo, parent) if parent != base else remote
    expected = (set(before) - set(deleted)) | set(uploads)
    if set(final) != expected:
        raise ValueError(f'Space inventory differs after the commit: missing {sorted(expected - set(final))[:20]}, '
                         f'unexpected {sorted(set(final) - expected)[:20]}')
    for name in expected:
        if name in uploads:
            if not matches(uploads[name], final[name]):
                raise ValueError('Uploaded file mismatch: ' + name)
        elif (final[name].blob_id, final[name].lfs) != (before[name].blob_id, before[name].lfs):
            raise ValueError('Unrelated Space file changed: ' + name)
    return final


def _space_logs(remote, runs):
    """Space path -> run of every Inspect log copy of the given published runs (never live/)."""
    found = {}
    for name in remote:
        run, _, rel = name.partition('/')
        if run in runs and rel and category(rel) == 'eval':
            found[name] = run
    return found


def switch(api, download, *, dataset_commit, operation, delete_operation=None, receipt=None, backup_dir=None,
           commit=False, remove_test=True, repo=DEFAULT_REPO, dataset=DATASET_REPO, cache_dir=None, now=None):
    """Stage 3: every published run's Inspect View reads its logs from the dataset at ``dataset_commit``.

    One Space commit pinned to the head adds ``signin.html`` at the root, inlines the fetch shim first into
    each published run's ``inspect-view/index.html`` (``RUN/inspect``, ``../../signin.html``), records every
    Space log copy under ``archive-manifest.json`` ``dataset_files`` (the copies stay in the Space until
    ``delete-eval``) with a ``dataset_switch`` record, and with ``remove_test`` deletes the ``logs-test/``
    area. Before committing, every log copy must be present in the dataset at that commit with the same size
    and SHA-256 (LFS) as the Space copy, and each page must differ from its original by the shim alone.
    Nothing else changes (verified)."""
    from site_style import themed
    from space_navigation import private_space_navigation
    if not re.fullmatch(r'[0-9a-f]{40}', dataset_commit or ''):
        raise ValueError('Expected a full dataset commit')
    info = api.repo_info(repo, repo_type='space', files_metadata=True)
    if not info.private:
        raise ValueError('Requires a private Space')
    if not api.repo_info(dataset, repo_type='dataset').private:
        raise ValueError('The logs dataset must be private')
    base = info.sha
    remote = remote_files(api, repo, base)
    root = json.loads(Path(download(repo, MANIFEST, repo_type='space', revision=base,
                                    cache_dir=cache_dir)).read_bytes())
    runs = published_runs(root)
    logs = _space_logs(remote, set(runs))
    stored = dataset_logs.dataset_inventory(api, dataset, dataset_commit)
    records, problems = {}, []
    for name, run in sorted(logs.items()):
        target = dataset_path(run, name[len(run) + 1:])
        entry, space = stored.get(target), remote[name]
        sha = space.lfs.sha256 if space.lfs else None
        if entry is None or sha is None or entry[1] != sha or entry[0] != space.size:
            problems.append(name)
            continue
        records[name] = {'repo': dataset, 'path': target, 'commit': dataset_commit, 'bytes': space.size,
                         'sha256': sha, 'category': 'eval'}
    if problems:
        raise ValueError('Space log copies without an identical dataset copy at that commit: ' + ', '.join(problems[:20]))
    uploads, originals, without_view = {}, {}, []
    for run in runs:
        page = f'{run}/inspect-view/index.html'
        if page not in remote:
            without_view.append(run)
            continue
        data = Path(download(repo, page, repo_type='space', revision=base, cache_dir=cache_dir)).read_bytes()
        if not matches(data, remote[page]):
            raise ValueError('Remote page differs from its record: ' + page)
        has_shim = dataset_logs.SHIM_MARKER in data.decode('utf-8', 'replace')
        if not any(r == run for r in logs.values()) and not has_shim:
            without_view.append(run)  # no log in the Space: nothing to switch
            continue
        new = dataset_logs.with_log_shim(data.decode('utf-8'), path=f'{run}/inspect', commit=dataset_commit,
                                         signin='../../' + dataset_logs.SIGNIN_FILE, repo=dataset).encode('utf-8')
        if not has_shim:
            stripped = re.sub(r'<script id="evallog-fetch"[^>]*>.*?</script>', '', new.decode('utf-8'), count=1,
                              flags=re.S)
            if stripped.encode('utf-8') != data:
                raise ValueError('Adding the shim would change more than the shim: ' + page)
        originals[page] = data
        if new != data:
            uploads[page] = new
    signin = private_space_navigation(themed(dataset_logs.signin_page())).encode('utf-8')
    if dataset_logs.SIGNIN_FILE not in remote or not matches(signin, remote[dataset_logs.SIGNIN_FILE]):
        uploads[dataset_logs.SIGNIN_FILE] = signin
    for name, data in uploads.items():
        if re.search(rb'(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}', data):
            raise ValueError('Credential-like content in ' + name)
        if len(data) >= 9.5 * 1024 * 1024:
            raise ValueError('A switched page would become LFS: ' + name)
    removed = sorted(n for n in remote if n.startswith(TEST_PREFIX + '/')) if remove_test else []
    date = (now or datetime.datetime.now(datetime.timezone.utc)).date().isoformat()
    files = dict(root['files'])
    for name in removed:
        files.pop(name, None)
    for name, data in uploads.items():
        files[name] = digest(data)
    dataset_files = dict(root.get('dataset_files', {}))
    for name, record in records.items():
        old = dataset_files.get(name)
        if old is not None and (old.get('path'), old.get('sha256')) != (record['path'], record['sha256']):
            raise ValueError('archive-manifest.json already records another dataset copy of ' + name)
        dataset_files[name] = {**record, **({'moved': old['moved']} if old and old.get('moved') else {})}
    switched = sorted(set(runs) - set(without_view))
    previous = root.get(SWITCH_KEY, {})
    new_root = {**root, 'files': files, 'total_bytes': sum(x['bytes'] for x in files.values()),
                'dataset_repo': dataset, 'dataset_files': dataset_files,
                SWITCH_KEY: {'date': previous.get('date', date), 'dataset': dataset, 'dataset_commit': dataset_commit,
                             'runs': switched, 'signin': dataset_logs.SIGNIN_FILE,
                             'space_logs_deleted': previous.get('space_logs_deleted', False)}}
    manifest = json.dumps(new_root, indent=2).encode()
    if not matches(manifest, remote[MANIFEST]):
        uploads[MANIFEST] = manifest
    result = {'operation': 'switch', 'repo': repo, 'dataset': dataset, 'dataset_commit': dataset_commit,
              'space_revision': base, 'runs': switched, 'runs_without_logs': without_view,
              'pages': sorted(n for n in uploads if n.endswith('/inspect-view/index.html')),
              'uploads': sorted(uploads), 'removed_test_files': len(removed), 'log_copies_recorded': len(records),
              'committed': False}
    if not commit:
        return result
    if receipt is None or backup_dir is None:
        raise ValueError('Committing requires a receipt and a backup directory')
    backup_dir = Path(backup_dir)
    for name, data in originals.items():
        target = backup_dir / 'original' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (backup_dir / 'original').mkdir(parents=True, exist_ok=True)
    (backup_dir / 'original' / MANIFEST).write_bytes(
        Path(download(repo, MANIFEST, repo_type='space', revision=base, cache_dir=cache_dir)).read_bytes())
    write_json(backup_dir / 'before-inventory.json', {
        'space_revision': base,
        'files': {n: {'blob_id': i.blob_id, 'lfs_sha256': i.lfs.sha256 if i.lfs else None, 'size': i.size}
                  for n, i in sorted(remote.items())}})
    write_json(receipt, {**result, 'status': 'planned; committing'})
    if not uploads and not removed:
        result.update(revision=base, status='verified', note='already switched')
        write_json(receipt, result)
        return result
    if removed and delete_operation is None:
        raise ValueError('Removing logs-test/ needs a delete operation factory')
    operations = [*(delete_operation(path_in_repo=n) for n in removed),
                  *(operation(path_in_repo=n, path_or_fileobj=d) for n, d in uploads.items())]
    try:
        revision, parent = _commit_guarded(
            api, repo, base, remote, operations,
            f'Switch {len(switched)} run(s) Inspect View to read logs from the private dataset '
            f'{dataset}@{dataset_commit[:8]}; add signin.html' + (f'; remove {TEST_PREFIX}/' if removed else ''))
        _verify_commit(api, repo, revision, parent, base, remote, uploads, removed)
    except Exception as error:
        write_json(receipt, {**result, 'status': 'failed: ' + type(error).__name__ + ': ' + str(error)[:500]})
        raise
    result.update(committed=True, revision=revision, parent=parent, status='verified')
    write_json(receipt, result)
    return result


def delete_eval(api, download, *, switch_receipt, delete_operation, operation, receipt=None, backup_dir=None,
                commit=False, purge=True, repo=DEFAULT_REPO, dataset=DATASET_REPO, cache_dir=None, now=None):
    """Stage 4: delete the Space copies of the Inspect logs of every switched run.

    Only after a verified switch (``switch_receipt`` status ``verified``), and only for Space log copies whose
    ``archive-manifest.json`` ``dataset_files`` record names a dataset copy present at its commit with the same
    size and SHA-256 as the Space copy, of a run whose Inspect View carries the shim pinned to that commit.
    One Space commit pinned to the head deletes them and updates the manifest (records leave ``files``; their
    ``dataset_files`` records get ``moved``); verified; then the freed LFS objects are permanently deleted
    without rewriting history. ``live/`` is never touched."""
    switched = json.loads(Path(switch_receipt).read_text())
    if switched.get('operation') != 'switch' or switched.get('status') != 'verified':
        raise ValueError('The switch receipt is not a verified switch; run and verify the switch first')
    info = api.repo_info(repo, repo_type='space', files_metadata=True)
    if not info.private:
        raise ValueError('Requires a private Space')
    if not api.repo_info(dataset, repo_type='dataset').private:
        raise ValueError('The logs dataset must be private')
    base = info.sha
    remote = remote_files(api, repo, base)
    root = json.loads(Path(download(repo, MANIFEST, repo_type='space', revision=base,
                                    cache_dir=cache_dir)).read_bytes())
    state = root.get(SWITCH_KEY)
    if not state:
        raise ValueError('archive-manifest.json has no dataset switch record')
    runs = set(state['runs'])
    logs = _space_logs(remote, runs)
    unswitched = sorted(set(_space_logs(remote, set(published_runs(root))).values()) - runs)
    if unswitched:
        raise ValueError('Published runs with Space logs but not switched: ' + ', '.join(unswitched))
    inventories, refused, records, shims = {}, [], {}, {}
    for run in sorted(set(logs.values())):
        page = f'{run}/inspect-view/index.html'
        data = Path(download(repo, page, repo_type='space', revision=base, cache_dir=cache_dir)).read_bytes()
        shims[run] = dataset_logs.shim_settings(data.decode('utf-8', 'replace'))
    for name, run in sorted(logs.items()):
        record = root.get('dataset_files', {}).get(name)
        space = remote[name]
        sha = space.lfs.sha256 if space.lfs else None
        if record is None or record.get('repo', dataset) != dataset or record.get('category') != 'eval':
            refused.append((name, 'no dataset record'))
            continue
        if shims[run] != (dataset, record['commit'], f'{run}/inspect'):
            refused.append((name, 'Inspect View shim does not read this dataset commit'))
            continue
        if record['commit'] not in inventories:
            inventories[record['commit']] = dataset_logs.dataset_inventory(api, dataset, record['commit'])
        entry = inventories[record['commit']].get(record['path'])
        if entry is None or (entry[0], entry[1]) != (record['bytes'], record['sha256']) \
                or (sha, space.size) != (record['sha256'], record['bytes']):
            refused.append((name, 'dataset copy missing or different'))
            continue
        records[name] = {**record, 'space_lfs': space.lfs is not None}
    if refused:
        raise ValueError('Not deleted (nothing was): ' + '; '.join(f'{n}: {why}' for n, why in refused[:20]))
    date = (now or datetime.datetime.now(datetime.timezone.utc)).date().isoformat()
    files = {n: d for n, d in root['files'].items() if n not in records}
    dataset_files = dict(root['dataset_files'])
    for name in records:
        dataset_files[name] = {**dataset_files[name], 'moved': date}
    new_root = {**root, 'files': files, 'total_bytes': sum(x['bytes'] for x in files.values()),
                'dataset_files': dataset_files, SWITCH_KEY: {**state, 'space_logs_deleted': date}}
    manifest = json.dumps(new_root, indent=2).encode()
    result = {'operation': 'delete-eval', 'repo': repo, 'dataset': dataset, 'space_revision': base,
              'switch_revision': switched.get('revision'), 'runs': sorted(set(logs.values())),
              'delete': len(records), 'delete_bytes': sum(r['bytes'] for r in records.values()),
              'unique_lfs_bytes': sum({r['sha256']: r['bytes'] for r in records.values()}.values()),
              'used_storage_before': getattr(info, 'used_storage', None), 'committed': False}
    if not commit:
        return result
    if receipt is None or backup_dir is None:
        raise ValueError('Committing requires a receipt and a backup directory')
    if not records:
        result.update(status='verified', note='no Space log copies left')
        write_json(receipt, result)
        return result
    backup_dir = Path(backup_dir)
    write_json(backup_dir / 'before-inventory.json', {
        'space_revision': base,
        'files': {n: {'blob_id': i.blob_id, 'lfs_sha256': i.lfs.sha256 if i.lfs else None, 'size': i.size}
                  for n, i in sorted(remote.items())}})
    write_json(backup_dir / 'deleted.json', records)
    (backup_dir / 'original').mkdir(parents=True, exist_ok=True)
    (backup_dir / 'original' / MANIFEST).write_bytes(
        Path(download(repo, MANIFEST, repo_type='space', revision=base, cache_dir=cache_dir)).read_bytes())
    write_json(receipt, {**result, 'status': 'planned; deleting from the Space'})
    try:
        revision, parent = _commit_guarded(
            api, repo, base, remote,
            [*(delete_operation(path_in_repo=n) for n in sorted(records)),
             operation(path_in_repo=MANIFEST, path_or_fileobj=manifest)],
            f'Delete {len(records)} Inspect log copies of {len(result["runs"])} run(s) from the Space; Inspect View '
            f'reads them from the private dataset {dataset}')
        final = _verify_commit(api, repo, revision, parent, base, remote, {MANIFEST: manifest}, records)
        result.update(committed=True, revision=revision, parent=parent, status='committed and verified; purging')
        write_json(receipt, result)
        if purge:
            referenced = {i.lfs.sha256 for i in final.values() if i.lfs is not None}
            wanted = {r['sha256'] for r in records.values() if r['space_lfs']} - referenced
            objects = [f for f in api.list_lfs_files(repo, repo_type='space') if f.file_oid in wanted]
            if objects:
                api.permanently_delete_lfs_files(repo, objects, rewrite_history=False, repo_type='space')
            result.update(purged_lfs_objects=len(objects), purged_bytes=sum(f.size for f in objects))
        after = api.repo_info(repo, repo_type='space')
        result.update(status='verified', used_storage_after=getattr(after, 'used_storage', None))
    except Exception as error:
        result['status'] = 'failed: ' + type(error).__name__ + ': ' + str(error)[:500]
        write_json(receipt, result)
        raise
    write_json(receipt, result)
    return result


# --- test deployment (stage 3 test; production pages untouched) ------------------------------------------------
TEST_PREFIX = 'logs-test'


def test_landing(run):
    import html as _html
    r = _html.escape(run)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Logs sign-in test</title><style>:root{{--bg:#fff;--fg:#1f2328;--muted:#59636e;--accent:#0969da}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--accent:#4493f8}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}main{{max-width:720px;margin:0 auto;padding:32px 16px}}
a{{color:var(--accent)}}li{{margin:8px 0}}.muted{{color:var(--muted)}}</style></head><body><main>
<h1>Test: Inspect View reading logs from the private dataset</h1>
<p>This test copy of <strong>{r}</strong>'s Inspect View has no logs in the Space: it reads them from the private
dataset with your own HF sign-in. The production pages are unchanged.</p>
<ol><li><a href="signin.html?return=/{TEST_PREFIX}/{r}/inspect-view/index.html" target="_blank" rel="opener">Sign in with HF</a>
(opens a new tab; at HF's consent screen grant the organization <strong>gdm-math-swarm</strong>).</li>
<li>After signing in, that tab opens this run's Inspect View. It should list the log, open it and show the agents'
samples and transcripts like the production Inspect View.</li></ol>
<p class="muted">Without signing in (or in this frame, whose storage the browser keeps separate) the Inspect View shows a yellow
"Sign in with HF" bar instead of the log. <a href="{r}/inspect-view/index.html">Open the test Inspect View here anyway</a>.</p>
</main></body></html>
'''


def deploy_test(api, download, *, run, dataset_commit, operation, receipt=None, repo=DEFAULT_REPO,
                dataset=DATASET_REPO, cache_dir=None):
    """Add logs-test/: signin.html, a landing page and a copy of RUN's Inspect View (without .eval files) whose
    index.html reads the logs from the dataset at ``dataset_commit``. One guarded commit adds only logs-test/
    paths; nothing else in the Space changes (verified)."""
    from space_navigation import private_space_navigation
    info = api.repo_info(repo, repo_type='space', files_metadata=True)
    if not info.private:
        raise ValueError('Requires a private Space')
    base = info.sha
    remote = remote_files(api, repo, base)
    prefix = f'{run}/inspect-view/'
    uploads = {}
    for name in sorted(remote):
        if not name.startswith(prefix) or category(name[len(run) + 1:]) == 'eval':
            continue
        data = Path(download(repo, name, repo_type='space', revision=base, cache_dir=cache_dir)).read_bytes()
        if not matches(data, remote[name]):
            raise ValueError('Remote file differs from its record: ' + name)
        if name == prefix + 'index.html':
            data = dataset_logs.with_log_shim(data.decode('utf-8'), path=f'{run}/inspect', commit=dataset_commit,
                                              signin='../../signin.html', repo=dataset).encode('utf-8')
        uploads[f'{TEST_PREFIX}/{name}'] = data
    if f'{TEST_PREFIX}/{prefix}index.html' not in uploads:
        raise ValueError('The run has no published Inspect View')
    uploads[f'{TEST_PREFIX}/signin.html'] = private_space_navigation(
        dataset_logs.signin_page(f'/{TEST_PREFIX}/{prefix}index.html')).encode('utf-8')
    uploads[f'{TEST_PREFIX}/index.html'] = private_space_navigation(test_landing(run)).encode('utf-8')
    uploads[f'{TEST_PREFIX}/{dataset_logs.SHIM_FILE}'] = (dataset_logs.SITE / dataset_logs.SHIM_FILE).read_bytes()
    for name, data in uploads.items():
        if name.endswith(('.html', '.js')) and dataset_logs.re.search(rb'(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}', data):
            raise ValueError('Credential-like content in ' + name)
    changed = {n: d for n, d in uploads.items() if n not in remote or not matches(d, remote[n])}
    result = {'repo': repo, 'base_revision': base, 'run': run, 'dataset': dataset, 'dataset_commit': dataset_commit,
              'files': len(uploads), 'uploads': sorted(changed)}
    if changed:
        response = api.create_commit(repo_id=repo, repo_type='space', parent_commit=base,
                                     commit_message=f'Test area {TEST_PREFIX}/: {run} Inspect View reading logs from '
                                                    f'the private dataset (production pages unchanged)',
                                     operations=[operation(path_in_repo=n, path_or_fileobj=d) for n, d in changed.items()])
        revision = response.oid
    else:
        revision = base
    final = remote_files(api, repo, revision)
    expected = set(remote) | set(uploads)
    if set(final) != expected:
        raise ValueError('Unexpected Space inventory after the test deployment')
    for name in remote:
        if name not in uploads and (final[name].blob_id, final[name].lfs) != (remote[name].blob_id, remote[name].lfs):
            raise ValueError('A production file changed: ' + name)
    for name, data in uploads.items():
        if not matches(data, final[name]):
            raise ValueError('Uploaded test file differs: ' + name)
    result.update(revision=revision, verified=True)
    if receipt:
        write_json(receipt, result)
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Logs dataset stages (see deploy_test, switch, delete_eval)')
    parser.add_argument('command', choices=['deploy-test', 'switch', 'delete-eval'])
    parser.add_argument('--run', help='deploy-test: the run')
    parser.add_argument('--dataset-commit', help='deploy-test, switch: full dataset commit holding the logs')
    parser.add_argument('--switch-receipt', type=Path, help='delete-eval: the verified switch receipt')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--commit', action='store_true', help='switch, delete-eval: commit (default: plan only)')
    parser.add_argument('--keep-test', action='store_true', help='switch: keep the logs-test/ area')
    parser.add_argument('--no-purge', action='store_true', help='delete-eval: skip the LFS purge')
    args = parser.parse_args()
    from huggingface_hub import HfApi, CommitOperationAdd, CommitOperationDelete, hf_hub_download
    if args.command == 'deploy-test':
        if not args.run or not args.dataset_commit:
            parser.error('deploy-test needs --run and --dataset-commit')
        out = deploy_test(HfApi(), hf_hub_download, run=args.run, dataset_commit=args.dataset_commit,
                          operation=CommitOperationAdd, receipt=args.receipt)
    elif args.command == 'switch':
        if not args.dataset_commit:
            parser.error('switch needs --dataset-commit')
        out = switch(HfApi(), hf_hub_download, dataset_commit=args.dataset_commit, operation=CommitOperationAdd,
                     delete_operation=CommitOperationDelete, receipt=args.receipt, backup_dir=args.backup_dir,
                     commit=args.commit, remove_test=not args.keep_test)
    else:
        if not args.switch_receipt:
            parser.error('delete-eval needs --switch-receipt')
        out = delete_eval(HfApi(), hf_hub_download, switch_receipt=args.switch_receipt,
                          delete_operation=CommitOperationDelete, operation=CommitOperationAdd, receipt=args.receipt,
                          backup_dir=args.backup_dir, commit=args.commit, purge=not args.no_purge)
    print(json.dumps(out, indent=2))
