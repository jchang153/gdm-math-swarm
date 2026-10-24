#!/usr/bin/env python3
"""Read-only check that a published run's pages are served as HTML by the private HF Space.

    python scripts/verify_hosted.py --run RUN_ID --revision SHA [--bundle PREPARED_BUNDLE] [--output FILE]

Uses the operator's own HF login through the SDK; nothing is uploaded or changed. It checks that
the Space is private and its current revision is ``--revision`` (the hosted app serves the current
revision only), that each page is stored as a regular Git file, not LFS (HF serves LFS-tracked HTML
as a download), and that each hosted page returns HTTP 200 as ``text/html``, not as an attachment,
with exactly the bytes stored at that revision once HF's injected creator-metadata script is
removed. With ``--bundle`` the stored files are also compared with the prepared bundle. An optional
``--range-path`` (for example a run's Inspect ``.eval`` log) must answer a byte-range request with 206.
No served page may link a raw ``RUN/inspect/*.eval`` log (site-nologs, October 21, 2026: the link was
only a download; the logs stay published for Inspect View). Pages published earlier fail this check
until they are re-rendered.

Dataset checks (on when the run's Inspect View carries the dataset fetch shim, or with --dataset-checks;
docs/experiment-library.md, "Logs and raw records in the dataset repo"): the logs dataset is private;
the shim names the expected dataset, the folder RUN/inspect and a full commit (--dataset-commit if
given); every log of the run's inspect-view/logs/listing.json answers a Range read of its first and last
64 bytes through the dataset's resolve URL (operator's own login) with 206, compared with --bundle or the
Space copy when one exists; a CORS preflight from the Space origin allows authorization and range; no
checked page links a file recorded as moved to the dataset (archive-manifest.json dataset_files or
offline_files) directly, every repointed link (data-dataset-file) names a file present in the dataset at
its commit, and every same-site link of a checked page names a file stored in the Space
(no_broken_links). No RUN/analysis/timeline.md stays in the Space (no_space_timeline; the timeline lives
only in the dataset, owner, October 23, 2026) and every Timeline link of RUN/results.html points to the
dataset file at RUN/analysis/timeline.md (resolved by dataset_links_resolve) or to the page's "Raw records
kept offline" notice (timeline_link). --expect-no-space-logs also requires that the Space stores no .eval for the run.

Simplified behavior judge (scripts/simple_judge_site.py; with the default pages): none of its local-only working
files (cases.jsonl, raw/, run-budgets.json, adjudication.json) nor agents.jsonl (private dataset only) is stored in
the Space (simple_judge_local_only). When RUN/analysis/simple_judge/report.html is stored, it is checked like the
other pages, carries the comparability note (simple_judge_note), and RUN/results.html has the simplified judge's
Behavior judge tab (simple_judge_tab) and, when the run also has the rubric-3.1 judge, its "Behavior judge
(rubric 3.1)" tab (simple_judge_rubric_3_1_tab).
Exits 1 when any check fails; the record lists every check.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_hf_library import DEFAULT_REPO  # noqa: E402
import dataset_logs  # noqa: E402
from dataset_logs import DATASET_REPO  # noqa: E402
import simple_judge_site  # noqa: E402

# Runs published before October 21, 2026 also carry {run}/transcripts/index.html; pass it with --page
# to check it. New runs have no custom transcript pages (read agent transcripts in Inspect View).
PAGES = ("index.html", "{run}/results.html", "{run}/conference-view/index.html", "{run}/inspect-view/index.html")
# HF injects this one script into served HTML; remove only it before comparing bytes. With hf_oauth (Space
# revision 882dd1ff and later) it also carries OAUTH_CLIENT_ID, OAUTH_SCOPES and OPENID_PROVIDER_URL.
CREATOR_SCRIPT = re.compile(rb'<script>window\.huggingface=\{variables:\{(?:"[A-Z_]+":"[^"<\\]*",?)+\}\};</script>')
LFS_POINTER = b"version https://git-lfs.github.com/spec/"
PATHS_INFO_BATCH = 200
# A link to a raw Inspect log under RUN/inspect/ (the catalogue's and run page's former download links).
LOG_DOWNLOAD_LINK = re.compile(rb'href="[^"]*\binspect/[^"/]*\.eval"')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify(api, session, headers, repo, run, revision, pages=None, bundle=None, range_path=None, cache_dir=None,
           dataset_repo=DATASET_REPO, dataset_commit=None, dataset_checks=None, expect_no_space_logs=False):
    simple_judge_checks = pages is None
    pages = [page.format(run=run) for page in (pages or PAGES)]
    served = {}
    record = {"repo": repo, "run": run, "requested_revision": revision, "checks": [], "ok": False}

    def check(name, ok, **detail):
        record["checks"].append({"check": name, "ok": bool(ok), **detail})
        return ok

    info = api.space_info(repo)
    record.update(revision=info.sha, private=info.private, host=getattr(info, "host", None))
    check("private", info.private is True)
    if not check("current_revision", bool(revision) and str(info.sha).startswith(revision), current=info.sha):
        record["note"] = "The hosted app serves the current revision; pages were not compared."
        return record
    sj = f"{run}/{simple_judge_site.SJ_DIR}/"
    sj_page, old_judge = sj + "report.html", f"{run}/{simple_judge_site.OLD_REPORT}"
    sj_present = set()
    if simple_judge_checks:
        local_only = [sj + name for name in ("cases.jsonl", "raw", "run-budgets.json", "adjudication.json",
                                             *simple_judge_site.DATASET_FILES)]
        sj_present = {entry.path for entry in api.get_paths_info(repo, [sj_page, old_judge, *local_only],
                                                                 repo_type="space", revision=info.sha)}
        stray = sorted(sj_present & set(local_only))
        check("simple_judge_local_only", not stray, examples=stray)
        if sj_page in sj_present and sj_page not in pages:
            pages.append(sj_page)
    names = pages + ([range_path] if range_path else [])
    lfs = {}
    for start in range(0, len(names), PATHS_INFO_BATCH):  # the Hub refuses very long path lists (HTTP 413)
        lfs.update({entry.path: getattr(entry, "lfs", None) for entry in api.get_paths_info(
            repo, names[start:start + PATHS_INFO_BATCH], repo_type="space", revision=info.sha)})
    for page in pages:
        if not check("stored", page in lfs, path=page):
            continue
        check("not_lfs", lfs[page] is None, path=page)
        stored = Path(api.hf_hub_download(repo, page, repo_type="space", revision=info.sha,
                                          cache_dir=cache_dir)).read_bytes()
        if bundle is not None:
            local = Path(bundle) / page
            check("bundle_match", local.is_file() and sha256(local.read_bytes()) == sha256(stored), path=page)
        response = session.get(f"{info.host}/{page}", headers=headers, timeout=45, follow_redirects=True)
        kind = response.headers.get("content-type", "")
        disposition = response.headers.get("content-disposition", "")
        body = CREATOR_SCRIPT.sub(b"", response.content, count=1)
        served[page] = body
        check("served_html", response.status_code == 200 and kind.startswith("text/html")
              and "attachment" not in disposition.lower() and not response.content.startswith(LFS_POINTER),
              path=page, status=response.status_code, content_type=kind, content_disposition=disposition or None)
        check("served_exact", body == stored, path=page, served_sha256=sha256(body), stored_sha256=sha256(stored))
        check("no_log_download_link", not LOG_DOWNLOAD_LINK.search(body), path=page)
    if sj_page in sj_present:
        results = served.get(f"{run}/results.html", b"").decode("utf-8", "replace")
        check("simple_judge_tab", simple_judge_site.TAB in results, path=f"{run}/results.html")
        if old_judge in sj_present:
            check("simple_judge_rubric_3_1_tab", simple_judge_site.OLD_TAB in results, path=f"{run}/results.html")
        check("simple_judge_note", 'id=sj-comparability' in served.get(sj_page, b"").decode("utf-8", "replace"),
              path=sj_page)
    if range_path:
        response = session.get(f"{info.host}/{range_path}", headers={**headers, "Range": "bytes=0-63"}, timeout=45,
                               follow_redirects=True)
        ok = response.status_code == 206 and len(response.content) == 64
        if ok and bundle is not None and (Path(bundle) / range_path).is_file():
            ok = response.content == (Path(bundle) / range_path).read_bytes()[:64]
        check("range_206", ok, path=range_path, status=response.status_code)
    inspect_page = f"{run}/inspect-view/index.html"
    settings = None
    if inspect_page in lfs:
        stored = Path(api.hf_hub_download(repo, inspect_page, repo_type="space", revision=info.sha,
                                          cache_dir=cache_dir)).read_bytes()
        settings = dataset_logs.shim_settings(stored.decode("utf-8", "replace"))
    if dataset_checks is None:
        dataset_checks = settings is not None
    if dataset_checks or expect_no_space_logs:
        verify_dataset(api, session, headers, repo, run, info, check, settings, served, bundle=bundle,
                       range_path=range_path, cache_dir=cache_dir, dataset_repo=dataset_repo,
                       dataset_commit=dataset_commit, dataset_checks=dataset_checks,
                       expect_no_space_logs=expect_no_space_logs)
    record["ok"] = all(item["ok"] for item in record["checks"])
    return record


_DATASET_LINK = re.compile(r'href="https://huggingface\.co/datasets/([^/"]+/[^/"]+)/(?:blob|resolve)/([0-9a-f]{40})/'
                           r'([^"#?]+)"[^>]*data-dataset-file="')
_SAME_SITE = re.compile(r'''\b(?:href|src)=["']([^"'#?]*)''', re.I)


def _log_names(listing):
    """.eval file names of an Inspect View logs/listing.json (a mapping keyed by log or a list of entries)."""
    names = listing.keys() if isinstance(listing, dict) else [
        x.get("name", "") if isinstance(x, dict) else str(x) for x in listing or []]
    return sorted({PurePosixPath(str(n).split("?")[0]).name for n in names if str(n).split("?")[0].endswith(".eval")})


def verify_dataset(api, session, headers, repo, run, info, check, settings, served, *, bundle, range_path, cache_dir,
                   dataset_repo, dataset_commit, dataset_checks, expect_no_space_logs):
    tree = {item.path for item in api.list_repo_tree(repo, repo_type="space", revision=info.sha, recursive=True)
            if hasattr(item, "blob_id")}
    if expect_no_space_logs:
        stored_logs = sorted(n for n in tree if n.startswith(run + "/") and n.endswith(".eval"))
        check("no_space_logs", not stored_logs, examples=stored_logs[:5])
    if not dataset_checks:
        return
    dataset = api.repo_info(dataset_repo, repo_type="dataset")
    check("dataset_private", dataset.private is True, dataset=dataset_repo)
    expected_path = f"{run}/inspect"
    ok = (settings is not None and settings[0] == dataset_repo and settings[2] == expected_path
          and re.fullmatch(r"[0-9a-f]{40}", settings[1] or "") is not None
          and (dataset_commit is None or settings[1].startswith(dataset_commit)))
    check("inspect_shim", ok, path=f"{run}/inspect-view/index.html",
          shim=list(settings) if settings else None, expected_commit=dataset_commit)
    commit = settings[1] if ok else None
    listing_name = f"{run}/inspect-view/logs/listing.json"
    names = []
    if listing_name in tree:
        listing = json.loads(Path(api.hf_hub_download(repo, listing_name, repo_type="space", revision=info.sha,
                                                      cache_dir=cache_dir)).read_bytes())
        names = _log_names(listing)
    if range_path and range_path.endswith(".eval"):
        names = sorted(set(names) | {PurePosixPath(range_path).name})
    check("logs_listed", bool(names), path=listing_name, logs=names)
    if commit and names:
        paths = [f"{expected_path}/{name}" for name in names]
        sizes = {entry.path: entry.size for entry in api.get_paths_info(dataset_repo, paths, repo_type="dataset",
                                                                          revision=commit)}
        for name, path in zip(names, paths):
            if not check("dataset_log_stored", path in sizes, path=path, commit=commit):
                continue
            size = sizes[path]
            local = Path(bundle) / f"{run}/inspect/{name}" if bundle is not None else None
            if local is not None and not local.is_file():
                local = Path(bundle) / "dataset-files" / path
            if local is not None and local.is_file():
                data = local.read_bytes()
                head, tail, source = data[:64], data[-64:], "bundle"
            elif f"{run}/inspect/{name}" in tree or f"{run}/inspect-view/logs/{name}" in tree:
                space_path = f"{run}/inspect/{name}" if f"{run}/inspect/{name}" in tree else f"{run}/inspect-view/logs/{name}"
                url = f"{info.host}/{space_path}"
                head = dataset_logs.ranged_read(session, headers, url, 0, min(63, size - 1))[1]
                tail = dataset_logs.ranged_read(session, headers, url, max(0, size - 64), size - 1)[1]
                source = "space copy"
            else:
                head = tail = None
                source = "zip structure"
            if head is None:
                url = dataset_logs.file_url(path, commit, dataset_repo, "resolve")
                first = dataset_logs.ranged_read(session, headers, url, 0, 63)
                last = dataset_logs.ranged_read(session, headers, url, max(0, size - 64), size - 1)
                result = {"first_status": first[0], "last_status": last[0],
                          "ok": first[0] == 206 and last[0] == 206 and first[1].startswith(b"PK\x03\x04")
                          and b"PK\x05\x06" in last[1]}
            else:
                result = dataset_logs.check_ranges(session, headers, dataset_repo, commit, path, head, tail, size)
            check("range_206", result.pop("ok"), path=path, compared_with=source, **result)
        preflight = dataset_logs.check_preflight(session, info.host, dataset_repo, commit, paths[0])
        check("preflight", preflight.pop("ok"), path=paths[0], **preflight)
    manifest_name = "archive-manifest.json"
    moved = set()
    if manifest_name in tree:
        manifest = json.loads(Path(api.hf_hub_download(repo, manifest_name, repo_type="space", revision=info.sha,
                                                       cache_dir=cache_dir)).read_bytes())
        for key in ("dataset_files", "offline_files"):
            moved |= {n[len(run) + 1:] for n in (manifest.get(key) or {}) if n.startswith(run + "/")}
    # The activity timeline lives only in the private dataset (owner, October 23, 2026): no Space copy, and the
    # run page's Timeline link reaches the dataset file (resolved below) or the offline notice.
    timeline = f"{run}/{dataset_logs.TIMELINE_MD}"
    check("no_space_timeline", timeline not in tree, path=timeline)
    results_page = f"{run}/results.html"
    if results_page in served:
        text = served[results_page].decode("utf-8", "replace")
        anchors = [a for a in re.findall(r"<a\b[^>]*>", text) if dataset_logs.TIMELINE_MD in a]
        to_dataset = [a for a in anchors if f'data-dataset-file="{dataset_logs.TIMELINE_MD}"' in a
                      and re.search(r'href="https://huggingface\.co/datasets/' + re.escape(dataset_repo) + r'/blob/'
                                    r'[0-9a-f]{40}/' + re.escape(timeline) + '"', a)]
        offline = [a for a in anchors if f'data-offline-file="{dataset_logs.TIMELINE_MD}"' in a
                   and 'href="results.html#raw-records-offline"' in a]
        ok = len(to_dataset) + len(offline) == len(anchors) and (not offline or "id=\"raw-records-offline\"" in text)
        check("timeline_link", ok, path=results_page, links=len(anchors), dataset=len(to_dataset),
              offline=len(offline), examples=[a for a in anchors if a not in to_dataset + offline][:3])
    repointed = {}
    for page, body in served.items():
        text = body.decode("utf-8", "replace")
        if page.startswith(run + "/"):
            direct = dataset_logs.moved_links(text, page[len(run) + 1:], moved)
            check("no_moved_links", not direct, path=page, examples=direct[:5])
        for found_repo, found_commit, found_path in _DATASET_LINK.findall(text):
            repointed.setdefault((found_repo, found_commit), set()).add(unquote(found_path))
        base = PurePosixPath(page).parent
        broken = []
        for url in _SAME_SITE.findall(text):
            if not url or re.match(r"[a-z][a-z0-9+.-]*:|//", url, re.I) or url.endswith("/"):
                continue
            target = dataset_logs._normalise(base / unquote(url))
            if target is None or target not in tree:
                broken.append(url)
        check("no_broken_links", not broken, path=page, examples=sorted(set(broken))[:5])
    for (found_repo, found_commit), paths in sorted(repointed.items()):
        present = set()
        if found_repo == dataset_repo:
            ordered = sorted(paths)
            for start in range(0, len(ordered), PATHS_INFO_BATCH):
                present |= {entry.path for entry in api.get_paths_info(
                    found_repo, ordered[start:start + PATHS_INFO_BATCH], repo_type="dataset", revision=found_commit)}
        missing = sorted(paths - present)
        check("dataset_links_resolve", not missing, repo=found_repo, commit=found_commit, links=len(paths),
              examples=missing[:5])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--run", required=True, help="published run ID")
    parser.add_argument("--revision", required=True, help="expected current Space revision (full or prefix)")
    parser.add_argument("--bundle", help="prepared bundle directory to compare stored files with")
    parser.add_argument("--page", action="append", help="page to check ({run} expands); default: the run's views")
    parser.add_argument("--range-path", help="Space path that must answer a byte-range request with 206")
    parser.add_argument("--cache-dir", help="download cache (default: the HF cache)")
    parser.add_argument("--dataset-repo", default=DATASET_REPO, help="private logs dataset (default %(default)s)")
    parser.add_argument("--dataset-commit", help="expected dataset commit (full or prefix) in the Inspect View shim")
    parser.add_argument("--dataset-checks", action=argparse.BooleanOptionalAction, default=None,
                        help="dataset checks (default: on when the run's Inspect View carries the dataset shim)")
    parser.add_argument("--expect-no-space-logs", action="store_true",
                        help="also require that the Space stores no .eval for the run (after their deletion)")
    parser.add_argument("--output", help="also write the record as JSON here")
    args = parser.parse_args(argv)
    from huggingface_hub import HfApi
    from huggingface_hub.utils import build_hf_headers, get_session
    record = verify(HfApi(), get_session(), build_hf_headers(token=True), args.repo, args.run, args.revision,
                    args.page, args.bundle, args.range_path, args.cache_dir, dataset_repo=args.dataset_repo,
                    dataset_commit=args.dataset_commit, dataset_checks=args.dataset_checks,
                    expect_no_space_logs=args.expect_no_space_logs)
    text = json.dumps(record, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(text)
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
