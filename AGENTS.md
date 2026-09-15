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
