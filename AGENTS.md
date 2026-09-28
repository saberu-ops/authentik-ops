# authentik-ops Agent Instructions

Portable behavior is owned by `~/workspace/.agents/rules/`. This file holds only
authentik-ops project facts and constraints.

## Project Facts

- Deployment repository for the authentik stack serving `auth.saberu.app`. On a
  deployment host, `/opt/authentik` is intended to be a Git checkout of this
  repository; secrets and runtime state stay host-local and Git-ignored.
- Human-facing topology and security boundaries: [README.md](README.md).
  Install, update, upgrade, backup, restore, secret rotation, conversion, and
  rollback procedures: [RUNBOOK.md](RUNBOOK.md).
- Branch: `main`. Intended remote: `saberu-ops/authentik-ops` (private).
- Static gate: `scripts/check.sh`, which also runs `scripts/check_docs.py`.
  Run it after changing compose files, `Caddyfile`, `.env.example`, `scripts/`,
  `systemd/`, or Markdown. It needs Docker Compose and python3 and starts one
  offline `caddy validate` container unless run with `--no-containers`.

## Project Documents

- Human-facing documents are in Simplified Chinese; this file stays in English.
  [docs/README.md](docs/README.md) lists every document under `docs/` with its
  role. README.md and RUNBOOK.md describe only implemented, verified behavior.
  Plans describe intended behavior.
- Plans are `docs/plans/NNNN-<slug>.md`, with a `- 状态：` line near the top
  using the shared planning statuses. Revise a DRAFT in place instead of
  creating a sibling copy.
- Review, research, and decision records are `docs/reviews/NNNN-<slug>.md`.
  - Save one only when it does one of these:
    - changes a plan's design, scope, or acceptance;
    - records an operator decision;
    - must hand off across sessions or agents.
  - Write adopted decisions into the owning plan in the same change. The record
    keeps the rationale and evidence.
  - When a record is superseded, add a one-line pointer at its top to the
    current owner.
- Plan and review numbers are separate sequences and are never reused. Plans
  0001–0003 and reviews 0001–0010 were used by discarded drafts, so new
  numbers start at plan 0004 and review 0011. In prose, always write the type
  with the number, for example 方案 0004 or 评审 0011.
- A plan's final verified outcome record (Changelog) is
  `docs/changelogs/NNNN-<slug>.md` and uses the plan's number.
- A review freezes its inputs by Git commit, plus sha256 for uncommitted files,
  and quotes the passages it relies on. File modification times are not
  evidence.
- Implementation progress lives only in
  [docs/implementation/checklist.md](docs/implementation/checklist.md). Actual
  operations and their results go to
  [docs/implementation/records.md](docs/implementation/records.md).
- Raw logs and sensitive evidence stay outside Git. Documents keep a redacted
  summary and say where the evidence is kept.
- `apply` is the instance command that makes configuration take effect. Always
  write it as `apply`, never as "应用", which collides with authentik
  Applications.
- `scripts/check_docs.py` checks the following:
  - links and anchors;
  - docs-index coverage;
  - numbered filenames and their minimum numbers;
  - plan status lines;
  - that every changelog matches a plan.

## Constraints

- Keep the invariants enforced by `scripts/check.sh`. Relaxing one, or adding
  hardening beyond them, is an operator decision that needs explicit approval
  for that item.
- Privilege model: read-only inspection uses non-privileged surfaces
  (`systemctl show`, `docker compose ps`, health endpoints). `sudo`, starting or
  removing containers, pulling images, creating Compose projects, networks, or
  volumes, syncing files into `/opt/authentik`, and changing systemd units are
  host mutations that need an explicit per-operation request, including
  isolated test projects and `scripts/check.sh` on a deployment host.
- Never read or print values from a live `.env`, `.env.*.bak`, `caddy-data/`,
  `/root/.ssh`, or backup archives; compare by key names or hashes only.
- Never suggest `git clean -x`, `git clean -X`, or `git stash --all` in a
  deployment checkout; they remove ignored secrets and data.
