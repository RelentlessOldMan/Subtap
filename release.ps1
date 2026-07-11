<#
  Cut a Subtap release in one shot: bump __version__ (patch) in subtap.py -> commit + tag + push
  -> create the GitHub release with subtap.py attached (people download one file, no build).

  Commit your actual changes first (this only commits the version bump), then run:
    powershell -ExecutionPolicy Bypass -File .\release.ps1            # bump patch (1.0.0 -> 1.0.1)
    powershell -ExecutionPolicy Bypass -File .\release.ps1 1.1.0      # explicit version

  Needs: gh (authenticated) and a clean working tree.
#>
param([string]$Version)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 1. Require a clean tree so the tag captures your committed work (not half-finished edits).
if (git status --porcelain) { throw "Working tree not clean - commit or stash your changes first, then re-run." }

# 2. Find the current version in subtap.py and decide the new one.
$py = [System.IO.File]::ReadAllText("$PSScriptRoot\subtap.py")
if ($py -notmatch '__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"') { throw "Couldn't find __version__ in subtap.py" }
if ($Version) {
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must look like X.Y.Z" }
    $ver = $Version
} else {
    $ver = "{0}.{1}.{2}" -f $Matches[1], $Matches[2], ([int]$Matches[3] + 1)
}

# 3. Write the bumped version back (preserving the file exactly, UTF-8 no BOM).
$py = [regex]::Replace($py, '__version__\s*=\s*"\d+\.\d+\.\d+"', "__version__ = `"$ver`"")
[System.IO.File]::WriteAllText("$PSScriptRoot\subtap.py", $py)

# 4. Commit the bump, tag it, push both.
git add subtap.py
git commit --quiet -m "Release v$ver"
git tag "v$ver"
git push --quiet origin main
git push --quiet origin "v$ver"

# 5. Create the GitHub release with subtap.py attached.
$notes = @"
**Subtap v$ver** - a single-file, dependency-free caption timing editor (waveform, tap-sync, deltas).

No install: download subtap.py below and run:  python subtap.py

See the README for usage: https://github.com/RelentlessOldMan/Subtap#readme
"@
gh release create "v$ver" subtap.py --title "Subtap v$ver" --notes $notes

Write-Host "`nReleased v$ver -> https://github.com/RelentlessOldMan/Subtap/releases/tag/v$ver"
