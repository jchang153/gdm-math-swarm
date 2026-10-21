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
Exits 1 when any check fails; the record lists every check.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_hf_library import DEFAULT_REPO  # noqa: E402

# Runs published before October 21, 2026 also carry {run}/transcripts/index.html; pass it with --page
# to check it. New runs have no custom transcript pages (read agent transcripts in Inspect View).
PAGES = ("index.html", "{run}/results.html", "{run}/conference-view/index.html", "{run}/inspect-view/index.html")
# HF injects this one script into served HTML; remove only it before comparing bytes.
CREATOR_SCRIPT = re.compile(rb'<script>window\.huggingface=\{variables:\{"SPACE_CREATOR_USER_ID":"[0-9a-f]+"\}\};</script>')
LFS_POINTER = b"version https://git-lfs.github.com/spec/"
# A link to a raw Inspect log under RUN/inspect/ (the catalogue's and run page's former download links).
LOG_DOWNLOAD_LINK = re.compile(rb'href="[^"]*\binspect/[^"/]*\.eval"')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify(api, session, headers, repo, run, revision, pages=None, bundle=None, range_path=None, cache_dir=None):
    pages = [page.format(run=run) for page in (pages or PAGES)]
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
    lfs = {entry.path: getattr(entry, "lfs", None) for entry in
           api.get_paths_info(repo, pages + ([range_path] if range_path else []), repo_type="space", revision=info.sha)}
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
        body = CREATOR_SCRIPT.sub(b"", response.content)
        check("served_html", response.status_code == 200 and kind.startswith("text/html")
              and "attachment" not in disposition.lower() and not response.content.startswith(LFS_POINTER),
              path=page, status=response.status_code, content_type=kind, content_disposition=disposition or None)
        check("served_exact", body == stored, path=page, served_sha256=sha256(body), stored_sha256=sha256(stored))
        check("no_log_download_link", not LOG_DOWNLOAD_LINK.search(body), path=page)
    if range_path:
        response = session.get(f"{info.host}/{range_path}", headers={**headers, "Range": "bytes=0-63"}, timeout=45,
                               follow_redirects=True)
        ok = response.status_code == 206 and len(response.content) == 64
        if ok and bundle is not None and (Path(bundle) / range_path).is_file():
            ok = response.content == (Path(bundle) / range_path).read_bytes()[:64]
        check("range_206", ok, path=range_path, status=response.status_code)
    record["ok"] = all(item["ok"] for item in record["checks"])
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--run", required=True, help="published run ID")
    parser.add_argument("--revision", required=True, help="expected current Space revision (full or prefix)")
    parser.add_argument("--bundle", help="prepared bundle directory to compare stored files with")
    parser.add_argument("--page", action="append", help="page to check ({run} expands); default: the run's views")
    parser.add_argument("--range-path", help="Space path that must answer a byte-range request with 206")
    parser.add_argument("--cache-dir", help="download cache (default: the HF cache)")
    parser.add_argument("--output", help="also write the record as JSON here")
    args = parser.parse_args(argv)
    from huggingface_hub import HfApi
    from huggingface_hub.utils import build_hf_headers, get_session
    record = verify(HfApi(), get_session(), build_hf_headers(token=True), args.repo, args.run, args.revision,
                    args.page, args.bundle, args.range_path, args.cache_dir)
    text = json.dumps(record, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(text)
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
