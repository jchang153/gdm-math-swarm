# Experimental Claude Code agent image (gdm-math-swarm-agent:local).
#
# Build ONLY with an empty build context so no repository content, solutions,
# tests, researcher files, credentials or .env can enter the image:
#   mkdir -p /tmp/empty-context && docker build -f docker/agent.Dockerfile \
#     --build-arg LEAN_IMAGE=gdm-math-swarm-deps:local -t gdm-math-swarm-agent:local /tmp/empty-context
# There are deliberately no COPY/ADD instructions. Problem templates are written
# into each container at sample start (FrozenDataset.export_agent_tasks output).
ARG LEAN_IMAGE=gdm-math-swarm-deps:local
FROM ${LEAN_IMAGE}
USER root
ARG CLAUDE_CODE_VERSION=2.1.296
ARG NODE_MAJOR=22
ENV DEBIAN_FRONTEND=noninteractive
# Node is needed only for the npm installer; Claude Code 2.1.x runs its native
# platform binary. Network is used at build time only (containers run with
# network_mode: none).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 bash findutils coreutils procps \
       util-linux \
    && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o /tmp/nodesource_setup.sh \
    && bash /tmp/nodesource_setup.sh && rm /tmp/nodesource_setup.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && test "$(claude --version | cut -d ' ' -f 1)" = "${CLAUDE_CODE_VERSION}" \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache /tmp/*
# The dependency image already made /opt/lean-project and /opt/elan world-readable
# (a recursive chmod here would copy the whole Mathlib tree into a new layer).
# Ubuntu 24.04 ships user "ubuntu" with uid/gid 1000; replace it with the agent user.
# The root-owned dependency checkouts trip git's "dubious ownership" check for
# uid 1000; Lake then misreads the remote URL as changed and tries to delete
# and re-clone Mathlib. safe.directory restores read-only `lake env`.
RUN git config --system --add safe.directory '*' \
    && (userdel -r ubuntu 2>/dev/null || true) && (groupdel ubuntu 2>/dev/null || true) \
    && groupadd -g 1000 researcher && useradd -m -s /bin/bash -u 1000 -g 1000 researcher \
    && test -d /opt/lean-project/.lake/packages/mathlib \
    && su researcher -c 'test -r /opt/lean-project/lakefile.toml && test -x /opt/elan/bin/lake'
# Private Lake project: build configuration only, with the precompiled read-only
# dependency tree (Mathlib + FormalConjecturesForMathlib) reached through
# symlinks. `lake env lean <file>` in /workspace matches the judge's command.
RUN mkdir -p /workspace/.lake /workspace/problems /tmp/w/memory/wiki /home/researcher/.claude \
    && cp /opt/lean-project/lakefile.toml /opt/lean-project/lean-toolchain /opt/lean-project/lake-manifest.json /workspace/ \
    && ln -s /opt/lean-project/.lake/packages /workspace/.lake/packages \
    && ln -s /opt/lean-project/.lake/build /workspace/.lake/build \
    && : > /workspace/RESEARCH.md \
    && chown -R 1000:1000 /workspace /tmp/w /home/researcher \
    && chmod 700 /home/researcher /tmp/w
# Local Lean compilation limit (docs/plan.md "Compilation time limit": 120 s per
# Lean compilation, local checks included; docs/runtime.md "Local compilation
# limit"). Root-owned wrappers in /usr/local/bin, FIRST on PATH, run the elan
# `lake`/`lean` under `timeout -v -s KILL`, exactly as the judge does, whether the
# command runs in the foreground, through the Bash tool's run_in_background, or
# detached with nohup/setsid. Lake prepends the toolchain's bin directory to PATH
# for the commands it runs, so in `lake env lean` the inner `lean` is the toolchain
# binary itself; the lake wrapper is therefore what bounds it (timeout without
# --foreground kills its whole process group, lake and lean included). Inside a
# wrapper, /opt/elan/bin goes first so nested lake/lean calls are not timed twice.
# Invoking the elan or toolchain binaries by absolute path bypasses the wrappers
# (documented). Changing this block requires rebuilding the agent image and
# re-running the Phase B runtime checks.
ARG LOCAL_LEAN_TIMEOUT_SECONDS=120
RUN timeout_bin="$(command -v timeout)" && test -n "${timeout_bin}" && command -v setsid >/dev/null \
    && for tool in lake lean; do \
         printf '#!/bin/sh\nPATH="/opt/elan/bin:$PATH"\nexport PATH\nexec %s -v -s KILL %s %s "$@"\n' \
           "${timeout_bin}" "${LOCAL_LEAN_TIMEOUT_SECONDS}" "${tool}" > "/usr/local/bin/${tool}" \
         && chown root:root "/usr/local/bin/${tool}" && chmod 0755 "/usr/local/bin/${tool}" \
         && test -x "/opt/elan/bin/${tool}" || exit 1; \
       done
ENV PATH=/usr/local/bin:/opt/elan/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
RUN test "$(command -v lake)" = /usr/local/bin/lake && test "$(command -v lean)" = /usr/local/bin/lean \
    && command -v claude >/dev/null && command -v python3 >/dev/null && command -v git >/dev/null \
    && su researcher -s /bin/sh -c 'PATH=/usr/local/bin:/opt/elan/bin:/usr/bin:/bin; \
         test "$(command -v lake)" = /usr/local/bin/lake && test ! -w /usr/local/bin/lake' \
    && (cd /workspace && lean --version && lake --version)
ENV HOME=/home/researcher \
    BASH_DEFAULT_TIMEOUT_MS=120000 \
    BASH_MAX_TIMEOUT_MS=120000 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_BUG_COMMAND=1 \
    DISABLE_NON_ESSENTIAL_MODEL_CALLS=1
USER 1000:1000
WORKDIR /workspace
CMD ["tail", "-f", "/dev/null"]
