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
non-merge commit since the previous tag, commits that as `Release vX.Y.Z`, tags
it, pushes the multi-architecture image to GHCR as `X.Y.Z`, `X.Y` and `latest`,
and creates the GitHub release.

If a step fails after the tag has been pushed, delete the tag and the release
commit, fix the problem and run the workflow again.

## Running a published image

The first release makes the image public-readable by default only if the
package is set to public: open the package page on GitHub once and switch its
visibility to public.

```yaml
services:
  bridge:
    image: ghcr.io/mathiasbk/openprinttag-spoolman:latest
    restart: unless-stopped
    environment:
      WEB_PORT: 7913
    ports:
      - "7913:7913"
    volumes:
      - ./data:/data:Z
```

`setup.sh` and the bundled `docker-compose.yml` still build from source, which
stays the supported way to run a checkout of the repository.
