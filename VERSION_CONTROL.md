# IntelAvatar Version Control and Release Guide

## Version Model

| Build line | Branch | Public version | Meaning of the third number |
|---|---|---|---|
| Nightly | `main` | `102.MINOR.PATCH` | First-parent commits since the latest main version-line change |
| Developer | `feature/*`, `fix/*` | `102.MINOR.PATCH-dev.SHA` | Main count at the feature branch point |
| Stable release | `release/X.Y` | `X.Y.PATCH` | Release-branch hotfix count |

Examples for the current cycle:

```text
main:         102.0.0, 102.0.1, 102.0.2
release/2.0:  2.0.0, 2.0.1, 2.0.2
feature/fix:  102.0.1-dev.a1b2c3d
```

The main and PV release numbers are independent: for example, main `102.1.1` and PV `2.0.1` may exist at the same time.

## GitHub Downloads

The repository home page contains direct links to both build lines:

- **Latest nightly:** the `nightly-latest` prerelease, updated after every successful push to `main`
- **Latest stable:** the newest non-prerelease GitHub Release from `release/X.Y`

The nightly workflow also uploads a GitHub Actions artifact. The prerelease ZIP is the recommended nightly download because it remains available from one stable link.

## Branches and Workflows

### `main`

`.github/workflows/build.yml` runs for:

- Pull requests targeting `main`: developer build
- Pushes to `main`: nightly build

Main builds read their major and minor from `configs/version_line.json`. The commit that changes this file is the `.0` baseline; each subsequent first-parent commit on main increments PATCH.

### `release/X.Y`

`.github/workflows/build-release.yml` runs when a commit reaches a branch matching `release/**`, for example `release/2.0`.

The branch name controls the PV major and minor components:

```text
release/2.0 -> 2.0.x
```

The release workflow builds the executable, creates a ZIP, publishes a stable GitHub Release, creates a tag such as `v2.0.1`, and uploads a workflow artifact.

Use `release/2.0`, not `release/v2.0`. The `v` prefix belongs on Git tags, not branch names.

### Feature and fix branches

Feature and fix branches produce developer versions such as:

```text
102.0.1-dev.a1b2c3d
```

The SHA identifies the exact source commit. These builds are posted to the pull request and are not stable releases.

## Starting a New Release Cycle

To start PV `2.0` and move main forward:

1. Open a PR changing `configs/version_line.json` to `main_major: 102`, `main_minor: 0`, and `next_pv_release: "2.0"`. Merge it to main; that commit builds as `102.0.0`.
2. Create and push `release/2.0` from that exact main commit:

   ```powershell
   git fetch origin --prune
   git switch main
   git pull origin main
   git switch -c release/2.0
   git push -u origin release/2.0
   ```
3. The release workflow publishes `v2.0.0`. Creating `release/2.0` also opens a PR advancing main to `102.1` and setting the next PV line to `2.1`.
4. Review and merge that version-bump PR. Its config-changing commit is the new `.0` baseline and builds as `102.1.0`.
5. Merge the next PR to main; it builds as `102.1.1`. Use squash merge or a single merge commit so each PR adds one first-parent commit.

For the generated version-bump PR to run `build.yml` automatically, configure the repository secret `VERSION_BOT_TOKEN` with a GitHub App or fine-grained token that can write contents and pull requests. Without it, the workflow falls back to `GITHUB_TOKEN`, which may not trigger follow-up workflow runs.

Expected builds:

```text
main:         102.0.0 -> 102.1.0 -> 102.1.1
release/2.0:  2.0.0   -> 2.0.1
```

Do not create `release/2.0` before the `102.0.0` baseline PR is merged. The release branch must start from the exact main baseline commit.

## Normal Development

After the cycle starts:

```text
main:         102.1.0 -> 102.1.1 -> 102.1.2
release/2.0:  2.0.0    -> 2.0.1   -> 2.0.2
```

The `main` number increases for commits in the nightly cycle. The release number increases only for approved hotfixes on the release branch.

## Critical Fixes and Cherry-Picks

When a critical fix already exists on `main`:

```powershell
git fetch origin
git switch release/2.0
git pull origin release/2.0
git switch -c hotfix/critical-fix
git cherry-pick <main-commit-sha>
git push -u origin hotfix/critical-fix
```

Open a pull request from `hotfix/critical-fix` to `release/2.0`. After it is merged, `build-release.yml` runs and publishes the next patch, for example `v2.0.1`.

The cherry-picked commit has a different Git commit ID on the release branch. That is expected and does not need to match `main`.

## Local Builds

Run the local build script from the repository root:

```powershell
.\build_with_version.ps1
```

The script mirrors CI:

| Current branch | Build type | Example |
|---|---|---|
| `main` | Nightly | `102.1.0` |
| `release/2.0` | Stable release | `2.0.0` |
| `feature/my-fix` | Developer | `102.1.0-dev.a1b2c3d` |

Do not manually edit `configs/version.py` or `version_info.txt`; the build process regenerates them.

## Troubleshooting

- A push to `main` runs `build.yml`, not `build-release.yml`.
- A push to `release/X.Y` runs `build-release.yml`.
- A push to a feature branch does not publish a stable release.
- A failed build does not create a valid stable release.
- Failed or skipped build numbers are not reused by rewriting Git history.
- Existing tags must not be moved after publication.
- If branch protection requires pull requests, direct pushes to `main` or a release branch will be rejected.

## Historical Note

Main and PV release numbering are independent. Release branches are cut from main after the main baseline is established; creating the configured next PV branch opens a reviewed PR for the next main minor baseline.
