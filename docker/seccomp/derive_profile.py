"""Derive the comparator seccomp profile from Docker's default profile.

Source: moby/moby tag docker-v29.8.1 (the engine version on the development host),
file vendor/github.com/moby/profiles/seccomp/default.json (moby/profiles/seccomp
v0.2.3), vendored here unchanged as moby-default-docker-v29.8.1.json with
SHA-256 UPSTREAM_SHA256 below.

Changes, and only these:
1. AF_UNIX (1) sockets are denied. Upstream allows ``socket`` for every family
   below 38. That rule is replaced by non-overlapping allow rules for family 0
   and families 2..37, plus an explicit ``socket(AF_UNIX, ...)`` rule returning
   EAFNOSUPPORT. ``socketpair`` (AF_UNIX-only on Linux) and ``socketcall``
   (the 32-bit multiplexer) are removed from the allow list and therefore fail
   with the profile default (EPERM). Neither is a native arm64/x86_64 syscall.
   Reason: landrun/Landlock cannot restrict connecting to pathname UNIX sockets
   before Landlock ABI v9 (Linux 7.1); the development host runs 7.0 (ABI 8).
2. ``ptrace``/``process_vm_readv``/``process_vm_writev`` are no longer allowed
   without CAP_SYS_PTRACE (which the container never has). Landlock already
   forbids ptrace out of a sandbox domain; this is defence in depth for the
   trusted entry process, which runs under the same uid.
3. Only the native 64-bit ABI of the two supported hosts is in the filter:
   ``archMap`` keeps the SCMP_ARCH_X86_64 and SCMP_ARCH_AARCH64 entries with
   no sub-architectures (upstream adds X86 + X32 and ARM). Docker puts the
   host's archMap entry and its sub-architectures into the filter
   (moby setupSeccomp); libseccomp then rewrites every ``socket`` rule on the
   32-bit X86 ABI into ``socketcall(SYS_SOCKET)`` and drops the family
   condition. With change 1 that makes the ALLOW and ERRNO rules collide on
   one chain (runc fails to load the filter, EEXIST); without the ERRNO rule
   it would admit AF_UNIX through ``int 0x80``. Excluding the compat ABIs makes
   every i386/x32/AArch32 syscall hit libseccomp's bad-architecture action,
   so ``socketcall`` is unreachable on both hosts. The comparator image
   contains only native 64-bit binaries. Other host architectures are
   unsupported (``audit_host`` covers amd64 and arm64 only).

Loading on a real x86_64 Docker host is verified only by the sandbox probe on
that host (docs/evaluator.md, "Seccomp"); this file's audit is structural.

Run: python3 docker/seccomp/derive_profile.py  (rewrites comparator-no-af-unix.json)
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM = HERE / 'moby-default-docker-v29.8.1.json'
OUTPUT = HERE / 'comparator-no-af-unix.json'
UPSTREAM_SHA256 = '536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74'
AF_UNIX = 1
EAFNOSUPPORT = 97
PTRACE_FAMILY = {'ptrace', 'process_vm_readv', 'process_vm_writev'}
# Syscalls that can create AF_UNIX sockets without the per-family socket filter.
UNFILTERED_SOCKET_CALLS = {'socketpair', 'socketcall'}
# Docker GOARCH -> libseccomp native architecture (moby nativeToSeccomp), supported hosts only.
NATIVE_ARCH = {'amd64': 'SCMP_ARCH_X86_64', 'arm64': 'SCMP_ARCH_AARCH64'}
SUPPORTED_HOSTS = tuple(NATIVE_ARCH)
# ABIs on which libseccomp rewrites `socket` rules into the `socketcall`
# multiplexer with arg0 == SYS_SOCKET, losing the family condition.
MULTIPLEXED_SOCKET_ARCHES = frozenset({'SCMP_ARCH_X86', 'SCMP_ARCH_S390', 'SCMP_ARCH_S390X', 'SCMP_ARCH_PPC',
                                       'SCMP_ARCH_PPC64', 'SCMP_ARCH_PPC64LE', 'SCMP_ARCH_MIPS', 'SCMP_ARCH_MIPSEL'})


def derive(upstream: dict) -> dict:
    profile = copy.deepcopy(upstream)
    rules = []
    replaced = False
    for rule in profile['syscalls']:
        names = rule['names']
        if (rule.get('action') == 'SCMP_ACT_ALLOW' and UNFILTERED_SOCKET_CALLS & set(names)
                and not rule.get('args')):
            rule = dict(rule, names=[n for n in names if n not in UNFILTERED_SOCKET_CALLS])
        if (names == ['socket'] and rule.get('action') == 'SCMP_ACT_ALLOW'
                and rule.get('args') == [{'index': 0, 'value': 38, 'op': 'SCMP_CMP_LT'}]):
            rules.append({'names': ['socket'], 'action': 'SCMP_ACT_ALLOW',
                          'args': [{'index': 0, 'value': AF_UNIX, 'op': 'SCMP_CMP_LT'}]})
            for family in range(AF_UNIX + 1, 38):
                rules.append({'names': ['socket'], 'action': 'SCMP_ACT_ALLOW',
                              'args': [{'index': 0, 'value': family, 'op': 'SCMP_CMP_EQ'}]})
            rules.append({'names': ['socket'], 'action': 'SCMP_ACT_ERRNO',
                          'errnoRet': EAFNOSUPPORT,
                          'args': [{'index': 0, 'value': AF_UNIX, 'op': 'SCMP_CMP_EQ'}]})
            replaced = True
            continue
        if (rule.get('action') == 'SCMP_ACT_ALLOW' and set(names) == PTRACE_FAMILY
                and not rule.get('includes', {}).get('caps')):
            continue
        rules.append(rule)
    if not replaced:
        raise ValueError('Upstream socket rule not found; re-audit the derivation')
    profile['syscalls'] = rules
    if profile.get('architectures'):
        raise ValueError('Upstream uses `architectures`; re-audit the derivation')
    natives = set(NATIVE_ARCH.values())
    arch_map = [{'architecture': e['architecture'], 'subArchitectures': None}
                for e in profile.get('archMap') or [] if e['architecture'] in natives]
    if {e['architecture'] for e in arch_map} != natives:
        raise ValueError('Upstream archMap lacks a supported architecture; re-audit the derivation')
    profile['archMap'] = arch_map
    return profile


def allows_af_unix(profile: dict) -> bool:
    """Structural audit: does any capability-free ALLOW rule admit AF_UNIX?"""
    for rule in profile['syscalls']:
        if rule.get('action') != 'SCMP_ACT_ALLOW' or rule.get('includes', {}).get('caps'):
            continue
        if UNFILTERED_SOCKET_CALLS & set(rule['names']):
            return True
        if 'socket' not in rule['names']:
            continue
        conditions = [a for a in rule.get('args') or [] if a['index'] == 0]
        if not conditions:
            return True
        for c in conditions:
            ok = {'SCMP_CMP_LT': AF_UNIX < c['value'], 'SCMP_CMP_LE': AF_UNIX <= c['value'],
                  'SCMP_CMP_EQ': AF_UNIX == c['value'], 'SCMP_CMP_GT': AF_UNIX > c['value'],
                  'SCMP_CMP_GE': AF_UNIX >= c['value'], 'SCMP_CMP_NE': AF_UNIX != c['value']}.get(c['op'], True)
            if ok:
                return True
    return False


def host_architectures(profile: dict, goarch: str) -> list:
    """Architectures in the filter Docker loads on a host with this GOARCH.

    moby setupSeccomp: `architectures` as given, or the archMap entry of the
    native architecture plus its subArchitectures; libseccomp always includes
    the native architecture.
    """
    native = NATIVE_ARCH[goarch]
    listed = list(profile.get('architectures') or [])
    if not listed:
        for entry in profile.get('archMap') or []:
            if entry['architecture'] == native:
                listed = [native] + list(entry.get('subArchitectures') or [])
                break
    return [native] + [a for a in listed if a != native]


def host_rules(profile: dict, goarch: str) -> list:
    """Rules Docker keeps on that host for a container without capabilities."""
    kept = []
    for rule in profile['syscalls']:
        includes, excludes = rule.get('includes') or {}, rule.get('excludes') or {}
        if includes.get('caps'):
            continue
        if includes.get('arches') and goarch not in includes['arches']:
            continue
        if goarch in (excludes.get('arches') or []):
            continue
        kept.append(rule)
    return kept


def audit_host(profile: dict, goarch: str) -> list:
    """Structural problems of the profile on one host architecture (empty: none found).

    Native ABI: no ALLOW rule admits AF_UNIX. Compat ABIs in the filter on
    which ``socket`` is multiplexed: any socket rule becomes an unconditioned
    ``socketcall(SYS_SOCKET)`` rule, so an ALLOW admits AF_UNIX and differing
    actions make the filter fail to load.
    """
    rules = host_rules(profile, goarch)
    problems = []
    if allows_af_unix(dict(profile, syscalls=rules)):
        problems.append('%s: an ALLOW rule admits AF_UNIX on the native ABI' % goarch)
    multiplexed = sorted(set(host_architectures(profile, goarch)) & MULTIPLEXED_SOCKET_ARCHES)
    actions = {(r.get('action'), r.get('errnoRet')) for r in rules if 'socket' in r['names']}
    if multiplexed and actions:
        if any(action == 'SCMP_ACT_ALLOW' for action, _ in actions):
            problems.append('%s: socket rules become socketcall(SYS_SOCKET) without the family condition on %s, '
                            'admitting AF_UNIX' % (goarch, ', '.join(multiplexed)))
        if len(actions) > 1:
            problems.append('%s: conflicting socketcall(SYS_SOCKET) actions on %s; the filter fails to load'
                            % (goarch, ', '.join(multiplexed)))
    return problems


def main():
    raw = UPSTREAM.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != UPSTREAM_SHA256:
        raise SystemExit(f'Unexpected upstream profile digest {digest}')
    profile = derive(json.loads(raw))
    problems = [p for goarch in SUPPORTED_HOSTS for p in audit_host(profile, goarch)]
    if allows_af_unix(profile) or problems:
        raise SystemExit('Derived profile fails the structural audit: ' + '; '.join(problems or ['AF_UNIX admitted']))
    OUTPUT.write_text(json.dumps(profile, indent=1) + '\n')
    print(OUTPUT, hashlib.sha256(OUTPUT.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
