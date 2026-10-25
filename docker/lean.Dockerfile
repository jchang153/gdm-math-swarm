# Operational judge compiler image: the precompiled dependency project, frozen,
# run as an unprivileged user on a read-only root filesystem.
# Build from an EMPTY context and pin the base by image ID, e.g.
#   docker build -f docker/lean.Dockerfile \
#     --build-arg LEAN_PROJECT_IMAGE=gdm-math-swarm-deps@sha256:<id> \
#     -t gdm-math-swarm-lean:local /tmp/empty-context
# then record `docker image inspect --format '{{.Id}}'` in the validation evidence
# (`image_ids`); rebuilding invalidates judge/declarations/timing records.
# Runtime requirements used by research_swarm.lean (present in the base, checked on
# the 2026-10-13 build): coreutils `timeout` (9.4; the in-container Lean limit
# `timeout -v -s KILL`), `sh`, `cat`, and python3 for the validation-only measured
# variant. The judge runs `lake env lean`, which does not apply lakefile leanOptions.
ARG LEAN_PROJECT_IMAGE=gdm-math-swarm-deps:local
FROM ${LEAN_PROJECT_IMAGE}
USER root
# The dependency checkouts are root-owned and the judge runs as uid 65534. Without
# safe.directory git reports "dubious ownership", Lake misreads the Mathlib remote
# as changed and tries to re-clone it (EROFS on the read-only judge rootfs). The
# deps image built before this line was added to dependencies.Dockerfile lacks
# it, so it is (idempotently) repeated here.
RUN mkdir -p /tmp && chmod 1777 /tmp && test -d /opt/lean-project/.lake \
    && git config --system --add safe.directory '*'
# No server, credentials, or submission source embedded in this image.
USER 65534:65534
WORKDIR /opt/lean-project
