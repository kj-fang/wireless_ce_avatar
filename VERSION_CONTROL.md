# IntelAvatar Version Control Guide

## Overview
Version control supports two build modes:
- **Development builds**: Fixed version with git SHA for per-build tracking
- **Release builds**: Auto-incrementing version based on commit count with git SHA

## Version Format

### Development Builds
`MAJOR.MINOR.PATCH-dev+SHA`
- Example: `1.0.0-dev+abc1234`
- Version number stays constant
- Each build tagged with unique git SHA

### Release Builds
`MAJOR.MINOR.PATCH+SHA`
- Example: `1.0.245+abc1234` (where 245 is the commit count on main branch)
- Version auto-increments with each merge to main
- Includes git SHA for traceability

---

## How It Works

### Development Build (Default)
Run without the `-Release` flag:
```powershell
.\build_with_version.ps1
```

This will:
1. Use static version `1.0.0-dev`
2. Append current git commit SHA
3. Build with PyInstaller
4. Output: `IntelAvatar_v1.0.0_dev_abc1234.exe`

### Release Build
Run with the `-Release` flag:
```powershell
.\build_with_version.ps1 -Release
```

This will:
1. Count commits on main branch
2. Use incremented version `1.0.N` (N = commit count)
3. Append current git commit SHA
4. Update `configs/version.py` with version info
5. Build with PyInstaller
6. Output: `IntelAvatar_v1.0.245_abc1234.exe`

### CI/CD (GitHub Actions)

**Development Builds (build.yml):**
- Triggers on: Pull requests and pushes to main
- Version format: `1.0.0-dev+SHA`
- Creates build artifacts for testing
- Comments on PRs with download links

**Release Builds (build-release.yml):**
- Triggers on: Pushes to main (or manual trigger)
- Version format: `1.0.N+SHA` (N = commit count)
- Creates GitHub Release with versioned artifacts
- Uploads build to releases page

Both workflows:
1. Calculate version with git SHA
2. Update version.py and version_info.txt
3. Build with PyInstaller
4. Create versioned executables

---

## Usage

### 1. Check Current Version
```python
from configs.version import __version__, BUILD_DATE, GIT_HASH, BUILD_TYPE
print(f"Version: {__version__}")
print(f"Build Type: {BUILD_TYPE}")
```

Or run the app - it displays on startup:
```
🚀 IntelAvatar v1.0.0-dev+abc1234 starting...
📅 Build: 2026-02-05 14:30:00
🔖 Git: abc1234 (feature-branch)
🔧 Build Type: Development
```

### 2. Build Locally

**Development Build (for testing/debugging):**
```powershell
# Default - creates dev build with fixed version + SHA
.\build_with_version.ps1

# Output: IntelAvatar_v1.0.0_dev_abc1234.exe
```

**Release Build (for production/distribution):**
```powershell
# Creates release build with incremented version + SHA
.\build_with_version.ps1 -Release

# Output: IntelAvatar_v1.0.245_abc1234.exe
```

### 3. Release Process
1. Make changes on a feature branch
2. Test locally with development builds: `.\build_with_version.ps1`
3. Create pull request to **main**
4. Merge PR to main
5. Build release version: `.\build_with_version.ps1 -Release`
6. Distribute the release build

---

## Version Increment Examples

### Development Builds (Default)
| Action | Version Output | Filename |
|--------|---------------|----------|
| First dev build | v1.0.0-dev+abc1234 | IntelAvatar_v1.0.0_dev_abc1234.exe |
| After making changes | v1.0.0-dev+def5678 | IntelAvatar_v1.0.0_dev_def5678.exe |
| Another dev build | v1.0.0-dev+ghi9012 | IntelAvatar_v1.0.0_dev_ghi9012.exe |

*Note: Version number stays at 1.0.0-dev, only SHA changes per commit*

### Release Builds (-Release flag)
| Action | Version Output | Filename |
|--------|---------------|----------|
| First release (50 commits) | v1.0.50+abc1234 | IntelAvatar_v1.0.50_abc1234.exe |
| Merge PR #1 to main | v1.0.51+def5678 | IntelAvatar_v1.0.51_def5678.exe |
| Merge PR #2 to main | v1.0.52+ghi9012 | IntelAvatar_v1.0.52_ghi9012.exe |

*Note: Version increments with each commit to main*

---

## Files Created

1. **configs/version.py** - Stores version info with BUILD_TYPE field (auto-updated)
2. **build_with_version.ps1** - Local build script with dev/release modes
3. **version_info.txt** - Windows executable metadata (auto-updated)
4. **.github/workflows/build.yml** - CI for development builds (auto-triggers on PRs)
5. **.github/workflows/build-release.yml** - CI for release builds (auto-triggers on main)

---

## Manual Version Override

### Change Base Version
To change the major/minor version, edit `build_with_version.ps1`:

**For Development builds:**
```powershell
# Line ~30
$version = "2.0.0-dev"  # Changed from 1.0.0-dev
```

**For Release builds:**
```powershell
# Line ~28
$version = "2.0.$commitCount"  # Changed from 1.0
```

---

## Quick Reference

| Command | Build Type | Version Format | Use Case |
|---------|-----------|----------------|----------|
| `.\build_with_version.ps1` | Development | 1.0.0-dev+SHA | Daily testing, debugging |
| `.\build_with_version.ps1 -Release` | Release | 1.0.N+SHA | Production distribution |

---

## Tips

### Build Type Comparison

| Aspect | Local Dev Build | Local Release Build | CI Dev Build | CI Release Build |
|--------|----------------|---------------------|--------------|------------------|
| **Command** | `.\build_with_version.ps1` | `.\build_with_version.ps1 -Release` | Auto on PR | Auto on push to main |
| **Version** | 1.0.0-dev+SHA | 1.0.N+SHA | 1.0.0-dev+SHA | 1.0.N+SHA |
| **Trigger** | Manual | Manual | Pull Request | Push to main |
| **Purpose** | Quick testing | Local release | PR validation | Official release |
| **Output** | Local dist/ | Local dist/ | PR artifact | GitHub Release |

### Best Practices

1. **Development builds** are perfect for:
   - Testing new features
   - Quick iterations
   - Sharing with team members for feedback
   - Each build has unique SHA for tracking

2. **Release builds** should be used for:
   - Official releases
   - Production deployments
   - Version tracking against bug reports
   - Customer distributions

3. Always commit your changes before building to ensure accurate SHA tracking

4. The git SHA provides full traceability back to the exact code state

### Dos and Don'ts

✅ **DO:**
- Let CI/CD handle releases automatically
- Keep main branch clean and stable
- Use feature branches for development

❌ **DON'T:**
- Manually edit `configs/version.py` (it gets overwritten)
- Push broken code to main
- Skip testing before merging to main
