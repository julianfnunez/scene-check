# GitHub and container publishing

Repository: https://github.com/julianfnunez/scene-check

Image: `ghcr.io/julianfnunez/scene-check`

The `.github/workflows/publish.yml` workflow runs on pull requests to `main`, pushes
to `main`, `v*` tags, and manual dispatch. It installs Python 3.12 dependencies from
`uv.lock`, prepares example configurations, runs pytest, builds an amd64 container,
and smoke-tests imports and configuration without starting a check or calling vendors.
Only successful `main` or version-tag runs publish the already-validated image.
Pull requests and manual runs on other branches build and test without publishing.

Tags:

- `latest`: successful main-branch builds.
- `sha-<commit>`: the build's source commit (short SHA).
- `1.2.3`: builds triggered by a version tag such as `v1.2.3`.

GitHub Actions authenticates with its generated `GITHUB_TOKEN` and `packages: write`;
no personal access token or OpenAI/camera/Hue/ntfy credentials are needed in CI.
Action versions are pinned to commit SHAs. Actions must be enabled for the repository;
organization policies may require allowing the actions used by this workflow.

## Configuration files

Commit `scenes.example.yaml` and `sensors.example.yaml`. Personal `scenes.yaml`,
`sensors.yaml`, `.env`, data, virtual environments, and local tooling directories are
ignored. The Dockerfile explicitly copies source and example configurations rather
than the whole checkout. Images contain placeholder device IDs, not the local IDs.
Local runtime files are left untouched; if a file was previously tracked, `.gitignore`
alone does not untrack it. Check staged files before committing existing repositories.

After a fresh checkout, prepare local configuration before running tests or the app:

```bash
cp scenes.example.yaml scenes.yaml
cp sensors.example.yaml sensors.yaml
cp .env.example .env
uv sync --frozen
uv run --frozen pytest -q
```

Fill in credentials in `.env`, camera environment variable names and door IDs in
`sensors.yaml`, and conditions/actions/schedules in `scenes.yaml`. Do not replace
existing runtime files when updating a deployment. Custom adapter programs outside
`mcp_servers/` need explicit Dockerfile COPY entries and their dependencies installed.

## First publication

Push the source and workflow to the repository's `main` branch, then open **Actions →
Test and publish container**. The image exists only after the container job succeeds.
Repository Settings → Actions controls whether workflows are enabled. If publishing
is denied, inspect repository/organization Actions policy and the package's repository
access settings. The image has a source label linking it to this repository.

New container packages are private by default. For unauthenticated deployment,
open the `scene-check` package's settings and make it public. To keep it private,
authenticate the deployment host with `docker login ghcr.io`, using a token authorized
to read the package. Do not put registry credentials in the application `.env`.

## Deploy or update on ZimaOS / Docker

Prepare `.env`, `scenes.yaml`, and `sensors.yaml` in the deployment directory and
keep the registry Compose file alongside them:

```bash
docker compose -f compose.registry.yaml pull
docker compose -f compose.registry.yaml up -d
```

Open `http://ZIMA_IP:8081`. The container retains checks and evidence in `./data` and
mounts the two runtime YAML files read-only. Its enabled schedules can perform checks
and send alerts after startup; the shipped example schedules `all_coops` at 21:15
America/New_York. Configure its sensors, actions, and schedule before starting it.

To deploy a fixed release or roll back, change the image tag in `compose.registry.yaml`
from `latest` to a published version or SHA tag, then pull and recreate. Updating the
image does not overwrite mounted configuration or data. The image targets linux/amd64,
matching ZimaBoard; additional architectures need a workflow change.

For source builds, use `docker compose up --build -d` instead.
