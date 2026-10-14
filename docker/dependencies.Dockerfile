# Dependency-only project. Build context must be data/vendor, not the repository.
# No FormalConjectures problem source trees or researcher reports are included.
# The base tag and elan-init script below are not digest-pinned; the built image
# ID is the pin of record (validation evidence `image_ids`; docs/judges.md
# "Image pinning"). The judge and agent images derive from this image, so a
# rebuild requires rebuilding them and re-running their validation.
# The base supplies coreutils `timeout`, used for the in-container Lean limit.
FROM ubuntu:24.04
USER root
ENV DEBIAN_FRONTEND=noninteractive ELAN_HOME=/opt/elan
ENV PATH=/opt/elan/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git zstd python3 bash \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o /tmp/elan-init.sh \
    && sh /tmp/elan-init.sh -y --default-toolchain none \
    && rm /tmp/elan-init.sh
COPY . /opt/lean-project/
WORKDIR /opt/lean-project
RUN elan toolchain install leanprover/lean4:v4.27.0 \
    && test "$(python3 -c 'import json; print(next(p["rev"] for p in json.load(open("lake-manifest.json"))["packages"] if p["name"]=="mathlib"))')" = a3a10db0e9d66acbebf76c5e6a135066525ac900 \
    && lake exe cache get \
    && test "$(git -C .lake/packages/mathlib rev-parse HEAD)" = a3a10db0e9d66acbebf76c5e6a135066525ac900 \
    && lake build FormalConjecturesForMathlib FormalConjectures.Util.ProblemImports \
    && chmod -R a+rX /opt/elan /opt/lean-project \
    && rm -rf /root/.cache/mathlib /root/.cache/elan
# Package checkouts are root-owned, but compilers/agents run as other users. Without
# this, git reports "dubious ownership", Lake reads an empty remote URL, decides
# "mathlib: URL has changed" and tries to delete and re-clone .lake/packages/mathlib
# (observed 2026-10-13; read-only rootfs made it fail with EROFS instead).
RUN git config --system --add safe.directory '*'
CMD ["lean", "--version"]
