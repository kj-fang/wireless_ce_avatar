# IntelAvatar Version Control Guide

## Overview
Version control is set up to **automatically increment** on every merge to the **main** branch using git commit count.

## Version Format
`MAJOR.MINOR.PATCH`
- Example: `1.0.245` (where 245 is the commit count on main branch)

---

## How It Works

### Local Building
Run the PowerShell build script:
```powershell
.\build_with_version.ps1
```

This will:
1. Count commits on main branch
2. Update `configs/version.py` with current version info
3. Build with PyInstaller
4. Output to `dist/IntelAvatar/`

### CI/CD (GitHub Actions)
When you push/merge to **main** branch:
1. GitHub Actions automatically triggers
2. Calculates version from commit count
3. Updates version.py
4. Builds the application
5. Creates a GitHub Release with the new version
6. Uploads build artifacts

---

## Usage

### 1. Check Current Version
```python
from configs.version import __version__, BUILD_DATE, GIT_HASH
print(f"Version: {__version__}")
```

Or run the app - it displays on startup:
```
🚀 IntelAvatar v1.0.245 starting...
📅 Build: 2026-02-05 14:30:00
🔖 Git: abc1234 (main)
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

## Version Increment Examples

| Action | Version Change |
|--------|---------------|
| Merge PR #1 to main | v1.0.1 |
| Merge PR #2 to main | v1.0.2 |
| Merge PR #3 to main | v1.0.3 |
| Dev branch commits | No change (not counted) |

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
