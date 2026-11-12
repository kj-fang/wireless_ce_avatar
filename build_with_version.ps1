# Local build script — mirrors the same 4-scenario version logic as CI:
#
#  Scenario 1 (PR build / local dev):  feature/* or fix/* branch
#    → version: 99.2.<cycle_count>-dev.<sha1>   BUILD_TYPE: DEV
#
#  Scenario 2 (nightly):  main branch
#    → version: 99.2.<cycle_count>               BUILD_TYPE: NIGHTLY
#
#  Scenario 3 & 4 (release candidate):  release/X.Y branch
#    → version: X.Y.<commit_count_on_branch>   BUILD_TYPE: RELEASE
#
# Run from repo root:  .\build_with_version.ps1

param(
    [string]$Branch = "main",
    # none (default): one IntelAvatar build, as CI makes it — the Beacon role
    # is decided at start (--handsfree-role / INTELAVATAR_HANDSFREE_ROLE).
    # client | server | both: one package per role, IntelAvatar_Client and/or
    # IntelAvatar_Server, with the role baked in (handsfree_role.txt in
    # _internal), the launcher .bat and README from tools/packaging/<role>
    # copied in, and each folder zipped for hand-out.
    [ValidateSet('none', 'client', 'server', 'both')]
    [string]$HandsfreeRole = "none"
)

# ── Nightly version line ──────────────────────────────────────────────────────
# To start a new nightly cycle, update $NightlyMinor and $NightlyBaseRef together.
# build.yml has matching NIGHTLY_MAJOR, NIGHTLY_MINOR, and NIGHTLY_BASE_REF values.
$NightlyMajor = 99
$NightlyMinor = 2
$NightlyBaseRef = "release/1.2"
# ─────────────────────────────────────────────────────────────────────────────

Write-Host "===== IntelAvatar Build Script with Version Control =====" -ForegroundColor Cyan

# Get git information
Write-Host "`nGetting version information from git..." -ForegroundColor Yellow

try {
    # Get short commit hash and current branch
    $gitHash = git rev-parse --short HEAD
    $currentBranch = git rev-parse --abbrev-ref HEAD
    $buildDate = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    if ($currentBranch -eq $Branch) {
        # ---- NIGHTLY build: on main branch ----
        # Version 99.2.x — the third number counts check-ins in this nightly cycle.
        git fetch origin $NightlyBaseRef --no-tags 2>$null
        $nightlyBase = git merge-base $Branch "origin/$NightlyBaseRef" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $nightlyBase) { throw "nightly cycle base unavailable" }
        $commitCount = git rev-list --count "${nightlyBase}..HEAD"
        $version = "$NightlyMajor.$NightlyMinor.$commitCount"
        $buildType = "NIGHTLY"
    } elseif ($currentBranch -match '^release/(\d+\.\d+)$') {
        # ---- RELEASE build: on a release/X.Y branch ----
        # PATCH = commits on this branch SINCE it diverged from main (not the global count).
        # This starts at 0 on branch cut and increments by 1 per hotfix commit.
        $releasePrefix = $Matches[1]   # e.g. "1.1"
        $releaseMajor  = ($releasePrefix -split '\.')[0]
        $releaseMinor  = ($releasePrefix -split '\.')[1]
        try {
            $mergeBase  = git merge-base HEAD $Branch 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $mergeBase) { throw "merge-base failed" }
            $patchCount = git rev-list --count "${mergeBase}..HEAD"
        } catch {
            # Fallback if merge-base is unavailable (shallow clone)
            $patchCount = git rev-list --count HEAD
            Write-Host "⚠️  Could not compute branch-point patch count — falling back to global count" -ForegroundColor Yellow
        }
        $version   = "$releaseMajor.$releaseMinor.$patchCount"
        $buildType = "RELEASE"
    } else {
        # ---- DEV build: on a feature/fix branch ----
        # Version 99.2.<cycle_count>-dev.<sha1>
        # cycle_count = commits in the nightly cycle at the branch point.
        try {
            $mergeBase = git merge-base HEAD $Branch 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $mergeBase) { throw "merge-base failed" }
            git fetch origin $NightlyBaseRef --no-tags 2>$null
            $nightlyBase = git merge-base $Branch "origin/$NightlyBaseRef" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $nightlyBase) { throw "nightly cycle base failed" }
            $baseCommitCount = git rev-list --count "${nightlyBase}..${mergeBase}"
        } catch {
            # Fallback: use the current main tip as the branch point.
            $mergeBase = git rev-parse $Branch
            git fetch origin $NightlyBaseRef --no-tags 2>$null
            $nightlyBase = git merge-base $Branch "origin/$NightlyBaseRef" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $nightlyBase) { throw "nightly cycle base failed" }
            $baseCommitCount = git rev-list --count "${nightlyBase}..${mergeBase}"
            Write-Host "⚠️  Could not determine branch point — using tip of '$Branch' as fallback (version may be approximate)" -ForegroundColor Yellow
        }
        $version = "$NightlyMajor.$NightlyMinor.$baseCommitCount-dev.$gitHash"
        $buildType = "DEV"
    }

    Write-Host "Build Type: $buildType" -ForegroundColor $(if ($buildType -eq "RELEASE") { "Green" } elseif ($buildType -eq "NIGHTLY") { "Cyan" } else { "Yellow" })
    Write-Host "Version: $version" -ForegroundColor Green
    Write-Host "Git Hash: $gitHash" -ForegroundColor Green
    Write-Host "Branch: $currentBranch" -ForegroundColor Green
    Write-Host "Build Date: $buildDate" -ForegroundColor Green

} catch {
    Write-Host "Error getting git information. Using defaults." -ForegroundColor Red
    $version = "$NightlyMajor.$NightlyMinor.0-dev.unknown"
    $gitHash = "unknown"
    $currentBranch = "unknown"
    $buildDate = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $buildType = "DEV"
}

# Update version.py
Write-Host "`nUpdating configs/version.py..." -ForegroundColor Yellow

$versionContent = @"
"""
Version information for IntelAvatar.
This file is auto-generated by the build script.
"""

__version__ = "$version"
BUILD_DATE = "$buildDate"
GIT_HASH = "$gitHash"
GIT_BRANCH = "$currentBranch"
"@

Set-Content -Path "configs/version.py" -Value $versionContent -Encoding UTF8
Write-Host "Version file updated successfully!" -ForegroundColor Green

# Update version_info.txt for exe metadata
Write-Host "`nUpdating version_info.txt for exe metadata..." -ForegroundColor Yellow

# Strip the -dev.<sha1> suffix for the numeric tuple (Windows exe metadata requires numbers only)
$numericVersion = $version -replace '-dev\..+$', ''
$versionParts = $numericVersion -split '\.'
$major = $versionParts[0]
$minor = $versionParts[1]
$patch = if ($versionParts.Length -gt 2 -and $versionParts[2]) { $versionParts[2] } else { "0" }

$versionInfoContent = @"
# UTF-8
#
# For more details about fixed file info 'ffi' see:
# http://msdn.microsoft.com/en-us/library/ms646997.aspx
VSVersionInfo(
  ffi=FixedFileInfo(
    # filevers and prodvers should be always a tuple with four items: (1, 2, 3, 4)
    # Set not needed items to zero 0.
    filevers=($major, $minor, $patch, 0),
    prodvers=($major, $minor, $patch, 0),
    # Contains a bitmask that specifies the valid bits 'flags'r
    mask=0x3f,
    # Contains a bitmask that specifies the Boolean attributes of the file.
    flags=0x0,
    # The operating system for which this file was designed.
    # 0x4 - NT and there is no need to change it.
    OS=0x40004,
    # The general type of file.
    # 0x1 - the file is an application.
    fileType=0x1,
    # The function of the file.
    # 0x0 - the function is not defined for this fileType
    subtype=0x0,
    # Creation date and time stamp.
    date=(0, 0)
    ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'Intel Corporation'),
        StringStruct(u'FileDescription', u'IntelAvatar - Wireless CE Log Analysis Tool'),
        StringStruct(u'FileVersion', u'$version.0'),
        StringStruct(u'InternalName', u'IntelAvatar'),
        StringStruct(u'LegalCopyright', u'Copyright © 2026 Intel Corporation'),
        StringStruct(u'OriginalFilename', u'IntelAvatar.exe'),
        StringStruct(u'ProductName', u'IntelAvatar'),
        StringStruct(u'ProductVersion', u'$version.0'),
        StringStruct(u'Comments', u'Git: $gitHash | Build: $buildDate')])
      ]), 
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"@

Set-Content -Path "version_info.txt" -Value $versionInfoContent -Encoding UTF8
Write-Host "version_info.txt updated successfully!" -ForegroundColor Green

# Build with PyInstaller — one variant (CI-style) or one package per Beacon role
Write-Host "`nBuilding with PyInstaller (HandsfreeRole: $HandsfreeRole)..." -ForegroundColor Yellow

$variants = switch ($HandsfreeRole) {
    'none'   { @(@{ Name = 'IntelAvatar';        Role = '' }) }
    'client' { @(@{ Name = 'IntelAvatar_Client'; Role = 'client' }) }
    'server' { @(@{ Name = 'IntelAvatar_Server'; Role = 'server' }) }
    'both'   { @(@{ Name = 'IntelAvatar_Client'; Role = 'client' },
                 @{ Name = 'IntelAvatar_Server'; Role = 'server' }) }
}

try {
    # Check if requirements are installed
    Write-Host "Installing/checking requirements..." -ForegroundColor Yellow
    pip install -r requirements.txt -q

    $built = @()
    foreach ($v in $variants) {
        $name = $v.Name
        # Read by IntelAvatar.spec: the exe/folder name and the baked-in role.
        $env:AVATAR_BUILD_NAME = $name
        $env:AVATAR_HANDSFREE_ROLE = $v.Role
        $roleNote = if ($v.Role) { " (Beacon role: $($v.Role))" } else { "" }
        Write-Host "`nRunning PyInstaller for $name$roleNote..." -ForegroundColor Yellow
        pyinstaller IntelAvatar.spec --noconfirm --workpath "build/$name"
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $name (exit $LASTEXITCODE)" }

        # Rename exe with version
        # For dev builds, $version already contains the SHA (e.g. 99.0.14-dev.b0530f21)
        # For nightly builds on main, append the short git hash
        # For release builds (release/X.Y), version is clean (e.g. 1.1.5) — no hash needed
        Write-Host "Renaming executable with version..." -ForegroundColor Yellow
        $exePath = "dist/$name/$name.exe"
        $exeName = if ($buildType -eq "RELEASE") { "${name}_v${version}.exe" } elseif ($buildType -eq "NIGHTLY") { "${name}_v${version}_${gitHash}.exe" } else { "${name}_v${version}.exe" }
        if (Test-Path $exePath) {
            Move-Item -Path $exePath -Destination "dist/$name/$exeName" -Force
            Write-Host "Renamed to: $exeName" -ForegroundColor Green
        }

        $zip = ""
        if ($v.Role) {
            # Hand-out extras: the launcher .bat and the README of this role.
            $extras = "tools/packaging/$($v.Role)"
            if (Test-Path $extras) {
                Copy-Item -Path "$extras/*" -Destination "dist/$name/" -Force
                Write-Host "Added launcher and README from $extras" -ForegroundColor Green
            }
            # The zip holds the folder itself, so unzipping never spills files.
            $zip = "dist/${name}_v${version}.zip"
            Write-Host "Zipping dist/$name -> $zip ..." -ForegroundColor Yellow
            if (Test-Path $zip) { Remove-Item $zip -Force }
            Compress-Archive -Path "dist/$name" -DestinationPath $zip -CompressionLevel Optimal
        }
        $built += [pscustomobject]@{ Name = $name; Exe = $exeName; Zip = $zip }
    }
    Remove-Item Env:AVATAR_BUILD_NAME, Env:AVATAR_HANDSFREE_ROLE -ErrorAction SilentlyContinue

    Write-Host "`n===== Build Complete! =====" -ForegroundColor Green
    Write-Host "Version: $version" -ForegroundColor Cyan
    Write-Host "Git Hash: $gitHash" -ForegroundColor Cyan
    foreach ($b in $built) {
        Write-Host "Executable: dist/$($b.Name)/$($b.Exe)" -ForegroundColor Cyan
        if ($b.Zip) { Write-Host "Package:    $($b.Zip)" -ForegroundColor Cyan }
    }

} catch {
    Write-Host "`nBuild failed: $_" -ForegroundColor Red
    exit 1
}
