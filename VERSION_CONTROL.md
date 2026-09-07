# IntelAvatar Version Control and Release Guide

## Version Model

| Build line | Branch | Public version | Meaning of the third number |
|---|---|---|---|
| Nightly | `main` | `99.CYCLE.CHECKIN` | Commits since the nightly cycle base |
| Developer | `feature/*`, `fix/*` | `99.CYCLE.CHECKIN-dev.SHA` | Nightly count at the feature branch point |
| Stable release | `release/X.Y` | `X.Y.PATCH` | Release-branch hotfix count |

Examples for the current cycle:

```text
main:         99.2.0, 99.2.1, 99.2.2
release/1.2:  1.2.0, 1.2.1, 1.2.2
feature/fix:  99.2.1-dev.a1b2c3d
```

The `99` major identifies non-stable builds. The nightly and release counters are intentionally independent: `99.2.8` and `1.2.2` are both valid at the same time.

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

Nightly builds use the configured cycle base and produce `99.2.<cycle_checkin>`. The current configuration uses `release/1.2` as the cycle-base reference for the `99.2` cycle.

### `release/X.Y`

`.github/workflows/build-release.yml` runs when a commit reaches a branch matching `release/**`, for example `release/1.2`.

The branch name controls the first two version components:

```text
release/1.2 -> 1.2.x
release/1.3 -> 1.3.x
```

The release workflow builds the executable, creates a ZIP, publishes a stable GitHub Release, creates a tag such as `v1.2.1`, and uploads a workflow artifact.

Use `release/1.2`, not `release/v1.2`. The `v` prefix belongs on Git tags, not branch names.

### Feature and fix branches

Feature and fix branches produce developer versions such as:

```text
99.2.1-dev.a1b2c3d
```

The SHA identifies the exact source commit. These builds are posted to the pull request and are not stable releases.

## Starting a New Release Cycle

For the next cycle, such as `1.3`, use this order:

1. Update the nightly cycle from `99.2` to `99.3` in both the GitHub Actions workflow and the local build script.
2. Commit the change on a branch with a message such as:

   ```text
   chore(version): start 99.3 nightly cycle
   ```

3. Open a pull request to `main` and merge it using the repository's branch-protection rules.
4. After the pull request is merged, create an immutable cycle-base reference from the resulting `main` commit. Prefer a tag such as `nightly-99.3-base`.
5. Create the stable branch from that exact `main` commit:

   ```powershell
   git fetch origin --prune
   git switch main
   git pull origin main
   git switch -c release/1.3
   git push -u origin release/1.3
   ```

6. Confirm the first builds are:

   ```text
   main:         99.3.0
   release/1.3:  1.3.0
   ```

Do not push the cycle-start commit independently to the release branch before it reaches `main`. A squash merge creates a new commit ID, so the cycle base must be created after the merge from the final `main` commit.

## Normal Development

After the cycle starts:

```text
main:         99.3.0 -> 99.3.1 -> 99.3.2
release/1.3:  1.3.0   -> 1.3.1   -> 1.3.2
```

The `main` number increases for commits in the nightly cycle. The release number increases only for approved hotfixes on the release branch.

## Critical Fixes and Cherry-Picks

When a critical fix already exists on `main`:

```powershell
git fetch origin
git switch release/1.3
git pull origin release/1.3
git switch -c hotfix/critical-fix
git cherry-pick <main-commit-sha>
git push -u origin hotfix/critical-fix
```

Open a pull request from `hotfix/critical-fix` to `release/1.3`. After it is merged, `build-release.yml` runs and publishes the next patch, for example `v1.3.1`.

The cherry-picked commit has a different Git commit ID on the release branch. That is expected and does not need to match `main`.

## Local Builds

Run the local build script from the repository root:

```powershell
.\build_with_version.ps1
```

The script mirrors CI:

| Current branch | Build type | Example |
|---|---|---|
| `main` | Nightly | `99.2.0` |
| `release/1.2` | Stable release | `1.2.0` |
| `feature/my-fix` | Developer | `99.2.0-dev.a1b2c3d` |

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

The `1.2` cycle exposed why a release branch should not be the permanent nightly anchor: cherry-picks and squash merges intentionally create different commit IDs. Future cycles should use an immutable cycle-base tag created from the final merged `main` commit, then create the release branch from that same commit.
