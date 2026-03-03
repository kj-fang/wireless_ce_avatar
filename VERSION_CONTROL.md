# IntelAvatar Version Control Guide

## Overview
Version numbers are **aligned between development and release** builds. A dev build always carries the version number of the release it was branched from, plus the current commit SHA1, so you can immediately tell which release a dev build corresponds to.

## Version Format

| Build type | Format | Example |
|------------|--------|---------|
| **Release** (main branch) | `MAJOR.MINOR.PATCH` | `1.0.245` |
| **Dev** (feature branch) | `MAJOR.MINOR.BASE_PATCH-dev.SHA1` | `1.0.245-dev.abc1234` |

- `PATCH` / `BASE_PATCH` = commit count on `main` at the branch point
- `SHA1` = short git hash of the current commit

### Example scenario
```
main:            ... o---o---o  (v1.0.245)
                              \
feature/my-fix:               o---o---o  (1.0.245-dev.f3c9e12)
```
When `feature/my-fix` is merged to main the release becomes `1.0.246`.

---

## How It Works

### Local Building
Run the PowerShell build script:
```powershell
.\build_with_version.ps1
```

This will:
1. Detect whether you are on `main` (release) or a feature branch (dev)
2. **On `main`**: version = `1.0.<commit_count>` (release version)
3. **On a feature branch**: version = `1.0.<base_release_commit_count>-dev.<short_SHA1>`
   - `base_release_commit_count` is the commit count on `main` at the point this branch diverged
4. Update `configs/version.py` with the computed version
5. Build with PyInstaller
6. Output to `dist/IntelAvatar/`

### CI/CD (GitHub Actions)

#### Pull Requests (`build.yml`)
When you open or update a PR to `main`:
1. GitHub Actions triggers the **dev** versioning path
2. Version = `1.0.<base_release_commit_count>-dev.<SHA1>`
3. Builds the application and uploads an artifact
4. Posts the dev version number as a PR comment

#### Merges to main (`build-release.yml`)
When you merge to `main`:
1. GitHub Actions triggers the **release** versioning path
2. Version = `1.0.<total_commit_count_on_main>`
3. Builds the application and creates a GitHub Release with a git tag

---

## Usage

### 1. Check Current Version
```python
from configs.version import __version__, BUILD_DATE, GIT_HASH
print(f"Version: {__version__}")
```

Or run the app — it displays on startup:
```
# Release build (on main)
🚀 IntelAvatar v1.0.245 starting...
📅 Build: 2026-02-05 14:30:00
🔖 Git: abc1234 (main)

# Dev build (on feature branch based on v1.0.245)
🚀 IntelAvatar v1.0.245-dev.f3c9e12 starting...
📅 Build: 2026-02-06 09:15:00
🔖 Git: f3c9e12 (feature/my-fix)
```

### 2. Build Locally
```powershell
# Build with version update
.\build_with_version.ps1

# Or if using a different branch
.\build_with_version.ps1 -Branch main
```

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
3. **.github/workflows/build-release.yml** - CI/CD automation

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
