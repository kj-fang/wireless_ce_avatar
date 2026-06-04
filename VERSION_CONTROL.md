# IntelAvatar Version Control Guide

## Overview

Two parallel version lines keep nightly development and stable releases clearly separated.

| Line | Branch | Version format | Example |
|------|--------|---------------|---------|
| **Nightly** | `main` | `99.0.PATCH` | `99.0.312` |
| **Release** | `release/X.Y` | `X.Y.PATCH` | `1.1.5` |
| **Dev** (feature branch off main) | `feature/*` / `fix/*` | `99.0.BASE-dev.SHA1` | `99.0.312-dev.f3c9e12` |

- `PATCH` on `main` / feature branches = total commit count on `main`
- `PATCH` on `release/X.Y` = total commit count on that release branch (starts near 0, increments with each hotfix)
- The `99` major makes it impossible to mistake a nightly build for a stable release

---

## Branching Model

```
main (99.0.x)    o--o--o--o--o--o--o--o--o--o--o--o--o-->  open to all
                          |                   |
                    release/1.1         release/1.2
                   o--o (hotfixes)      o (next cycle)
                   1.1.0  1.1.1  1.1.2  1.2.0 ...
```

### `main` branch
- Everyone pushes here directly (or via PR, per team preference)
- CI builds on every push → nightly artifact versioned `99.0.<commit_count>`

### `release/X.Y` branches
- Cut from `main` every **~4 weeks** by a maintainer
- **Locked** — no direct pushes; only hotfix PRs reviewed and approved before merge
- Version bumps to `X.Y.1`, `X.Y.2` etc. automatically with each merged hotfix
- When the next cycle begins, cut `release/X.(Y+1)` from `main`

### Feature / fix branches
- Branch from `main`, merge back to `main`
- Version: `99.0.<base_patch>-dev.<sha1>` — patch anchors to the nightly they branched from

---

## How to Cut a Release Branch

```powershell
# 1. Make sure main is up to date
git checkout main
git pull

# 2. Cut the release branch (change 1.1 to the new MAJOR.MINOR)
git checkout -b release/1.1

# 3. Push and set upstream
git push -u origin release/1.1
```

Then on GitHub/GitLab:
- Set **branch protection** on `release/1.1`:
  - Disable direct pushes
  - Require at least 1 PR approval
  - Optionally restrict who can merge

---

## How It Works

### Local Building
```powershell
.\build_with_version.ps1
```

| Current branch | Version produced | Build type |
|---------------|-----------------|------------|
| `main` | `99.0.<commit_count>` | `NIGHTLY` |
| `release/1.1` | `1.1.<commit_count_on_branch>` | `RELEASE` |
| `feature/my-fix` | `99.0.<base_patch>-dev.<sha1>` | `DEV` |

### CI/CD (GitHub Actions)

#### Pull Requests (`build.yml`)
Triggered on PRs to `main`:
- Version = `99.0.<base_patch>-dev.<SHA1>`
- Posts version as PR comment

#### Merges to `main` (`build.yml`)
- Version = `99.0.<commit_count>`
- Uploads nightly artifact (no GitHub Release)

#### Merges to `release/X.Y` (`build-release.yml`)
- Version = `X.Y.<commit_count_on_branch>`
- Creates a GitHub Release and git tag `vX.Y.<patch>`

---

## Usage

### Check Current Version
```python
from configs.version import __version__, BUILD_DATE, GIT_HASH
print(f"Version: {__version__}")
```

### Startup Output Examples
```
# Nightly (on main)
🚀 IntelAvatar v99.0.312 starting...
📅 Build: 2026-06-04 14:30:00
🔖 Git: abc1234 (main)

# Release (on release/1.1)
🚀 IntelAvatar v1.1.5 starting...
📅 Build: 2026-06-04 10:00:00
🔖 Git: def5678 (release/1.1)

# Dev (on feature branch)
🚀 IntelAvatar v99.0.312-dev.f3c9e12 starting...
📅 Build: 2026-06-05 09:15:00
🔖 Git: f3c9e12 (feature/my-fix)
```

---

## Release Cadence Summary

| Week | Action |
|------|--------|
| Week 0 | Cut `release/1.1` from `main` |
| Week 0–4 | Hotfixes only on `release/1.1`; `main` continues freely |
| Week 4 | Cut `release/1.2` from `main`; retire `release/1.1` |
| Repeat | `release/1.3`, `release/1.4` ... |


### 3. Release Process
1. Make changes on a feature branch
2. Create pull request to **main**
3. Merge PR to main
4. GitHub Actions automatically:
   - Increments version
   - Builds application
   - Creates release

---

## Version Alignment Examples

| Scenario | Version |
|----------|---------|
| Release merged to main (commit #245) | `v1.0.245` |
| Feature branch from `v1.0.245`, SHA `abc1234` | `1.0.245-dev.abc1234` |
| Another commit on same branch, SHA `f3c9e1` | `1.0.245-dev.f3c9e1` |
| Next release merged to main (commit #246) | `v1.0.246` |
| New feature branch from `v1.0.246`, SHA `d7e8f9` | `1.0.246-dev.d7e8f9` |

---

## Files Created

1. **configs/version.py** - Stores version info (auto-updated)
2. **build_with_version.ps1** - Local build script with versioning
3. **.github/workflows/build.yml** - CI/CD for PRs (scenarios 1) and main merges (scenario 2)
4. **.github/workflows/build-release.yml** - CI/CD for release branches only (scenarios 3 & 4)

---

## Manual Version Override

If you need to change the major/minor version:
1. In `build_with_version.ps1`, update the line that sets the `$version` value, for example:
   ```powershell
   $version = "2.0.$commitCount"  # Changed from 1.0

---

## Tips

✅ **DO:**
- Let CI/CD handle releases automatically
- Keep main branch clean and stable
- Use feature branches for development

❌ **DON'T:**
- Manually edit `configs/version.py` (it gets overwritten)
- Push broken code to main
- Skip testing before merging to main
