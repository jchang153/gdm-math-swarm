# Trusted independent evaluator image (researcher-only; never an agent image).
#
# Build (context is docker/, never the repository root, so .env/research/data
# are not sent to the builder):
#   docker build -f docker/comparator.Dockerfile \
#     --build-arg LEAN_PROJECT_IMAGE=gdm-math-swarm-deps:local \
#     -t gdm-math-swarm-comparator:local docker
#
# Pins (see docs/evaluator.md, "Pins"):
# * leanprover/comparator 14fda6a603d433dad5db83ca55f15afc91190fda — Lean v4.27.0,
#   export-format parser 2.0.0, Lean4Checker b7398199 (from its manifest).
# * leanprover/lean4export 9847384d3ea63a0e70567cdd6ae26c419402297c — the last
#   revision emitting export format 2.0.0. The previously recorded pin 06aa52f
#   emits NDJSON format 3.0.0, which comparator 14fda6a rejects ("Version
#   invalid"); comparator only adopted format 3 in 83a2c98 (Lean v4.28.0-rc1).
#   9847384 declares v4.27.0-rc1; it is compiled here with the v4.27.0
#   toolchain so that it reads the project's v4.27.0 .olean files.
# * Zouuup/landrun c91b41ac6cb180e2fdcb989408dcde34449bd8b0 (2026-01-25, reports
#   0.1.15): the last urfave/cli v2 revision, so comparator 14fda6a's argument
#   layout (no `--` before the command; `lean4export M -- decls`) is parsed as
#   upstream intended, and it contains the REFER fix lake needs for renames
#   under --rwx. v0.1.17 (cli v3) re-parses flags after the command and drops
#   the `--` lean4export needs; comparator only adapted to that in 9badaf4.
#   It applies Landlock V5 best-effort (no ABI-6 signal/abstract-socket scoping);
#   see docs/evaluator.md ("Sandbox") for the compensating controls. Built with pinned Go.
ARG LEAN_PROJECT_IMAGE=gdm-math-swarm-deps:local

FROM golang:1.24-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS landrun
ARG LANDRUN_REV=c91b41ac6cb180e2fdcb989408dcde34449bd8b0
RUN git clone https://github.com/Zouuup/landrun.git /src/landrun \
    && cd /src/landrun && git checkout --detach "$LANDRUN_REV" \
    && test "$(git rev-parse HEAD)" = "$LANDRUN_REV" \
    && CGO_ENABLED=0 GOFLAGS=-mod=readonly go build -trimpath -o /out/landrun ./cmd/landrun

FROM ${LEAN_PROJECT_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
ENV ELAN_HOME=/opt/elan
ARG LEAN_TOOLCHAIN=leanprover/lean4:v4.27.0
ARG COMPARATOR_REV=14fda6a603d433dad5db83ca55f15afc91190fda
ARG EXPORT_REV=9847384d3ea63a0e70567cdd6ae26c419402297c
COPY --from=landrun /out/landrun /usr/local/bin/landrun

RUN git clone https://github.com/leanprover/comparator.git /opt/comparator \
    && cd /opt/comparator && git checkout --detach "$COMPARATOR_REV" \
    && test "$(git rev-parse HEAD)" = "$COMPARATOR_REV" \
    && test "$(cat lean-toolchain)" = "$LEAN_TOOLCHAIN"

# Researcher addition: a separate executable built from the unmodified upstream
# Comparator library. It reproduces Main.lean's `verifyMatch` and `runKernel`
# (same targets, primitive list and Lean4Checker replay) but adds an opaque-aware
# permitted-axiom check. It reads two export files and reports which stage failed as one JSON line. It never
# runs untrusted code; the entry runs the sandboxed builds/exports first.
COPY --chmod=0644 GdmVerifyExports.lean /opt/comparator/GdmVerifyExports.lean

RUN cd /opt/comparator \
    && printf '\n[[lean_exe]]\nname = "gdm_verify_exports"\nroot = "GdmVerifyExports"\n' >> lakefile.toml \
    && lake build comparator gdm_verify_exports \
    && rm -rf .lake/packages/*/.git

RUN git clone https://github.com/leanprover/lean4export.git /opt/lean4export \
    && cd /opt/lean4export && git checkout --detach "$EXPORT_REV" \
    && test "$(git rev-parse HEAD)" = "$EXPORT_REV" \
    && grep -q 'def semver := "2.0.0"' Main.lean \
    && echo "$LEAN_TOOLCHAIN" > lean-toolchain \
    && lake build lean4export \
    && install -m 0755 .lake/build/bin/lean4export /usr/local/bin/lean4export

# Image-time manifest of pins; permission audit of the frozen dependency tree.
ARG LANDRUN_REV=c91b41ac6cb180e2fdcb989408dcde34449bd8b0
RUN mkdir -p /opt/gdm && printf '{"comparator":"%s","lean4export":"%s","landrun":"%s","landrun_version":"%s","toolchain":"%s","verifier_sha256":"%s"}\n' \
      "$COMPARATOR_REV" "$EXPORT_REV" "$LANDRUN_REV" "$(landrun --version 2>&1 | tail -n 1)" "$LEAN_TOOLCHAIN" "$(sha256sum /opt/comparator/GdmVerifyExports.lean | cut -d ' ' -f1)" \
      > /opt/gdm/pins.json \
    && git config --system --add safe.directory '*' \
    && chmod -R a+rX,go-w /opt/comparator /opt/lean4export /opt/gdm \
    && test -z "$(find /opt/lean-project /opt/elan -perm /022 -not -type l -print -quit)" \
    && mkdir -p /work && chown 65534:65534 /work
# Trusted entry last, so entry edits do not repeat the audit above. Every record
# reports this file's SHA-256 (entry_sha256); validation binds it to
# docker/comparator_entry.py, so an entry edit requires a rebuild and revalidation.
COPY --chmod=0644 comparator_entry.py /opt/gdm/comparator_entry.py
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["python3", "/opt/gdm/comparator_entry.py"]
