# Contributing

This is a research habitat, not a product. Small, reviewable changes to the harness, worldd, viewer, or docs are welcome.

## How to observe

1. Start with `./scripts/fresh.sh` then `./scripts/mac-up.sh`.
2. Open the glass URL printed at the end (`/?token=` from **your** `.env`).
3. Watch the roster, speech, corkboard, and file tree. That is the experiment.

Do not send PRs that dump a live tank: no `habitat/` after a run, no `private/*/STATE.md`, no `observatory.sqlite`, no `logs/`, no `.env`.

## Scope

In: worldd, fish harness, fetch proxy, viewer, seed corpus, run scripts, docs.

Out: sprites/3D, scoring, cloud-visitor hosting, mounting the keeper's home by default, `docker.sock`.
