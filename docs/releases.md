# Releasing

Everything is driven by GitHub Actions. There are three workflows:

| Workflow | When it runs | What it does |
| --- | --- | --- |
| `Tests` | every push to `main`/`development` and every pull request | runs `pytest` |
| `Docker image` | every push to `main`/`development`, build-only on pull requests | publishes `ghcr.io/mathiasbk/openprinttag-spoolman:edge` (main), `:dev` (development) and a `:sha-xxxxxxx` tag |
| `Release` | only when you start it by hand | tests, bumps the version, writes the changelog, tags, publishes the release and the versioned image |

## Cutting a release

1. Open **Actions → Release → Run workflow**.
2. Pick the branch (normally `main`) and whether this is a `patch`, `minor` or
   `major` release.
3. Press **Run workflow**.

The workflow then runs the tests, works out the next version from the newest
`vX.Y.Z` tag, writes it to `VERSION`, adds a `CHANGELOG.md` entry listing every
non-merge commit since the previous tag (the very first release just says "First tagged release"), commits that as `Release vX.Y.Z`, tags
it, pushes the multi-architecture image to GHCR as `X.Y.Z`, `X.Y` and `latest`,
and creates the GitHub release.

If a step fails after the tag has been pushed, delete the tag and the release
commit, fix the problem and run the workflow again.

## Running a published image

The bundled `docker-compose.yml` runs `ghcr.io/mathiasbk/openprinttag-spoolman:latest`,
so a server only needs that file, `.env` and `data/`. `./setup.sh` (or
`docker compose pull && docker compose up -d`) updates to the newest release.
Set `BRIDGE_TAG` in `.env` to pin a version (`0.1.0`), or to follow `main`
(`edge`) or `development` (`dev`).

The package must be public for this to work without logging in to GHCR: after
the first release, open the package page on GitHub once and set its visibility
to public.

`./setup.sh --build` builds from the checkout instead, using
`docker-compose.build.yml`.
