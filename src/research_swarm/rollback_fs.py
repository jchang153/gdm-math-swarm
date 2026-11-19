"""In-container half of the rollback file snapshots (research_swarm.rollback).

The agent containers run with ``cap_drop: [ALL]``, so root inside them cannot read the agent's
0700 directories; the snapshot therefore runs as the agent's own user, and nothing is stored in
the container: ``SCRIPT`` arrives on stdin (``BOOTSTRAP`` is the only command line), the host keeps
the object store and manifests (rollback.FileStore), and a restore is a plan the host sends back.
Nothing the agent reads is changed by a snapshot.

``scan``: every agent-owned entry under the roots (Claude Code's own state excluded except its
auto-memory; see rollback.SKIP_TOP / EXTRA_GLOBS), files hashed only when their (size, mtime, inode)
differ from the host's cache, and the content of every newly hashed file (base64). Files larger than
``max_file`` (or past the per-scan byte cap) are listed as skipped ("s") and left alone by a restore.

``apply``: a restore plan (delete, mkdir, write files, make links), each file written to a temporary
name and renamed into place with its mode and mtime. ``base`` maps "/" to a host folder (tests).
"""

BOOTSTRAP = "import sys,json;d=json.loads(sys.stdin.read());exec(compile(d['s'],'f','exec'),{'SPEC':d['p']})"

SCRIPT = r'''
import base64, glob, hashlib, json, os, shutil, stat, sys, time
BASE = SPEC.get("base") or ""
UID = int(SPEC["uid"])

def host(p):
    return BASE + p

def scan():
    roots, extra = SPEC["roots"], SPEC.get("extra_globs") or []
    skip_top, skip_names = SPEC.get("skip_top") or {}, set(SPEC.get("skip_names") or [])
    max_file, max_entries = int(SPEC["max_file"]), int(SPEC["max_entries"])
    budget = [int(SPEC["max_scan_bytes"])]
    cache = SPEC.get("cache") or {}
    out, blobs = {}, {}

    def entry(p, st):
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISDIR(st.st_mode):
            return ["d", mode, st.st_uid, st.st_gid]
        if stat.S_ISLNK(st.st_mode):
            return ["l", mode, st.st_uid, st.st_gid, os.readlink(host(p))]
        if not stat.S_ISREG(st.st_mode):
            return None
        key = [st.st_size, st.st_mtime_ns, st.st_ino]
        c = cache.get(p)
        if c and c[:3] == key:
            return ["f", mode, st.st_uid, st.st_gid, c[3], st.st_mtime_ns, st.st_size, st.st_ino]
        if st.st_size > max_file or st.st_size > budget[0]:
            return ["s", mode, st.st_uid, st.st_gid, st.st_size]
        try:
            with open(host(p), "rb") as f:
                data = f.read(max_file + 1)
        except OSError:
            return ["s", mode, st.st_uid, st.st_gid, st.st_size]
        if len(data) > max_file:
            return ["s", mode, st.st_uid, st.st_gid, len(data)]
        budget[0] -= len(data)
        sha = hashlib.sha256(data).hexdigest()
        blobs[sha] = base64.b64encode(data).decode()
        return ["f", mode, st.st_uid, st.st_gid, sha, st.st_mtime_ns, st.st_size, st.st_ino]

    def walk(root, include_root):
        try:
            st = os.lstat(host(root))
        except OSError:
            return
        if not stat.S_ISDIR(st.st_mode) or (include_root and st.st_uid != UID):
            return
        if include_root:
            out[root] = entry(root, st)
        top = tuple(skip_top.get(root) or ())
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                names = sorted(os.listdir(host(d)))
            except OSError:
                continue
            for name in names:
                if name in skip_names or (d == root and top and name.startswith(top)):
                    continue
                p = d.rstrip("/") + "/" + name
                try:
                    st = os.lstat(host(p))
                except OSError:
                    continue
                if st.st_uid != UID:
                    continue
                if len(out) >= max_entries:
                    raise RuntimeError("entry_limit")
                e = entry(p, st)
                if e is None:
                    continue
                out[p] = e
                if e[0] == "d":
                    stack.append(p)
    for root in roots:
        walk(root, False)
    for pattern in extra:
        for match in sorted(glob.glob(host(pattern))):
            walk(match[len(BASE):] if BASE else match, True)
    return {"entries": out, "blobs": blobs}

def remove(p):
    hp = host(p)
    if os.path.isdir(hp) and not os.path.islink(hp):
        shutil.rmtree(hp)
    elif os.path.lexists(hp):
        os.remove(hp)

def apply(plan):
    counts = {"deleted": 0, "written": 0, "dirs_created": 0, "links": 0}
    for p, is_dir in plan["delete"]:
        if is_dir:
            try:
                os.rmdir(host(p))
                counts["deleted"] += 1
            except OSError:
                pass  # still holds untracked (large or foreign) entries
        else:
            remove(p)
            counts["deleted"] += 1
    for p, mode in plan["dirs"]:
        hp = host(p)
        if os.path.lexists(hp) and (os.path.islink(hp) or not os.path.isdir(hp)):
            os.remove(hp)
        if not os.path.lexists(hp):
            os.makedirs(hp, exist_ok=True)
            counts["dirs_created"] += 1
        os.chmod(hp, mode)
    for p, kind, mode, mtime, payload in plan["files"]:
        hp = host(p)
        parent = os.path.dirname(hp)
        os.makedirs(parent, exist_ok=True)
        if os.path.isdir(hp) and not os.path.islink(hp):
            shutil.rmtree(hp)
        tmp = os.path.join(parent, ".%s.%d.rbtmp" % (os.path.basename(hp)[:40], os.getpid()))
        if os.path.lexists(tmp):
            os.remove(tmp)
        if kind == "l":
            os.symlink(payload, tmp)
            os.replace(tmp, hp)
            counts["links"] += 1
            continue
        with open(tmp, "wb") as f:
            f.write(base64.b64decode(payload))
        os.chmod(tmp, mode)
        os.utime(tmp, ns=(mtime, mtime))
        os.replace(tmp, hp)
        counts["written"] += 1
    return counts

began = time.monotonic()
try:
    if SPEC["op"] == "scan":
        out = scan()
    elif SPEC["op"] == "apply":
        out = apply(SPEC["plan"])
    else:
        raise RuntimeError("unknown_op")
    out = {"ok": True, **out, "seconds": round(time.monotonic() - began, 3)}
except Exception as ex:
    out = {"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:300])}
sys.stdout.write(json.dumps(out))
'''
