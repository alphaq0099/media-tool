param([switch]$Apply,[Parameter(Mandatory=$true)][ValidatePattern('^rc[0-9]+$')][string]$Candidate)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$artifactRoot = Join-Path $workspace 'artifacts'
$historyRoot = Join-Path $artifactRoot 'validation-history'
$evidenceRoot = Join-Path $artifactRoot 'ui-cleanup'
if (!(Test-Path -LiteralPath (Join-Path $workspace 'MediaTool.spec'))) { throw 'Wrong workspace.' }

function Get-Bytes([string]$Path) {
    $entry = Get-Item -LiteralPath $Path -Force
    if (!$entry.PSIsContainer) { return [long]$entry.Length }
    $sum = [long]0
    Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object { $sum += $_.Length }
    return $sum
}
function Assert-Safe([string]$Path) {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (!$resolved.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw "Outside workspace: $resolved" }
    $relative = [IO.Path]::GetRelativePath($workspace,$resolved)
    if ($relative -match '^(\.git|\.venv|docs|packaging|\.github)([\\/]|$)') { throw "Protected path: $relative" }
    $entries = @(Get-Item -LiteralPath $resolved -Force)
    if ($entries[0].PSIsContainer) { $entries += @(Get-ChildItem -LiteralPath $resolved -Recurse -Force) }
    if ($entries | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw "Link found; retained: $relative" }
    return $resolved
}
function Save-Reports([string]$Path) {
    $entry = Get-Item -LiteralPath $Path -Force
    $files = if ($entry.PSIsContainer) { @(Get-ChildItem -LiteralPath $Path -Recurse -Force -File) } else { @($entry) }
    foreach ($file in $files) {
        if ($file.Length -gt 2MB -or $file.Extension -notin @('.json','.xml','.md','.txt','.log')) { continue }
        if ($file.Name -match '^(ready-\d+|job-\d+|source-probe)\.json$') { continue }
        $within = if ($entry.PSIsContainer) { [IO.Path]::GetRelativePath($Path,$file.FullName) } else { $file.Name }
        $report = !$entry.PSIsContainer -or ($within -notmatch '[\\/]' -and $file.Name -match '(report|validation|acceptance|verification|performance|benchmark|manifest|regression|pytest|^tests\.|check|summary|comparison|package-size)') -or $within -match '^reports[\\/]'
        if (!$report) { continue }
        $destination = Join-Path $historyRoot ([IO.Path]::GetRelativePath($artifactRoot,$file.FullName))
        if (!$destination.StartsWith($historyRoot + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Report escaped history folder.' }
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
    }
}

$targets = [Collections.Generic.List[string]]::new()
foreach ($entry in Get-ChildItem -LiteralPath $artifactRoot -Force) {
    if ($entry.Name -in @('release-candidate','ui-cleanup','jobs','validation-history')) { continue }
    if (!$entry.PSIsContainer -and ($entry.Name -like "*-$Candidate-*" -or $entry.Name -like "$Candidate-*") -and $entry.Extension -in @('.json','.xml','.png','.zip','.sha256')) { continue }
    if ($entry.Name -match '\.(sqlite3|db)(-wal|-shm)?$') { continue }
    $targets.Add($entry.FullName)
}
foreach ($name in @('dist','build','.pytest_cache')) {
    $path = Join-Path $workspace $name
    if (Test-Path -LiteralPath $path) { $targets.Add($path) }
}
foreach ($area in @('src','tests','scripts')) {
    Get-ChildItem -LiteralPath (Join-Path $workspace $area) -Directory -Recurse -Force |
        Where-Object Name -eq '__pycache__' | ForEach-Object { $targets.Add($_.FullName) }
}
if (Test-Path -LiteralPath (Join-Path $workspace 'prototypes')) {
    Get-ChildItem -LiteralPath (Join-Path $workspace 'prototypes') -File -Recurse |
        Where-Object { $_.Extension -ne '.md' -and $_.Name -ne 'b-graphite-studio.png' } |
        ForEach-Object { $targets.Add($_.FullName) }
}
foreach ($name in @('check_phase1.py','check_documents_prototypes.py','remove_feedback_test_history.py','summarize_audio_documents.py','summarize_queue_completion.py','summarize_workflows.py','capture_feedback26.py')) {
    $path = Join-Path (Join-Path $workspace 'scripts') $name
    if (Test-Path -LiteralPath $path) { $targets.Add($path) }
}
Get-ChildItem -LiteralPath $evidenceRoot -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { $targets.Add($_.FullName) }
Get-ChildItem -LiteralPath $evidenceRoot -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -eq '.ini' -or $_.Name -like 'before-*' } |
    ForEach-Object { $targets.Add($_.FullName) }

$plan = @($targets | Select-Object -Unique | ForEach-Object {
    [pscustomobject]@{ path = [IO.Path]::GetRelativePath($workspace,$_); bytes = Get-Bytes $_ }
})
New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
$plan | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $evidenceRoot 'cleanup-plan.json') -Encoding utf8
if (!$Apply) {
    [pscustomobject]@{ targets = $plan.Count; removableMiB = [math]::Round(($plan | Measure-Object bytes -Sum).Sum / 1MB,2) } | ConvertTo-Json
    return
}

# A portable app may be in use even if its main window is hidden. Never remove
# a directory containing a running executable; no user process is stopped here.
$active = @(Get-Process -Name MediaTool -ErrorAction SilentlyContinue | ForEach-Object { $_.Path } | Where-Object { $_ })
$before = Get-Bytes $workspace
$removed = [Collections.Generic.List[string]]::new()
$retained = [Collections.Generic.List[object]]::new()
foreach ($target in $targets | Select-Object -Unique) {
    try {
        $safe = Assert-Safe $target
        if ($active | Where-Object { $_ -eq $safe -or $_.StartsWith($safe + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase) }) { throw 'Contains a running application.' }
        if ($safe.StartsWith($artifactRoot + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { Save-Reports $safe }
        Remove-Item -LiteralPath $safe -Recurse -Force
        $removed.Add([IO.Path]::GetRelativePath($workspace,$safe))
    } catch {
        $retained.Add([pscustomobject]@{ path = [IO.Path]::GetRelativePath($workspace,$target); reason = $_.Exception.Message })
    }
}
$after = Get-Bytes $workspace
$result = [pscustomobject]@{ beforeBytes=$before; afterBytes=$after; recoveredBytes=$before-$after; removed=$removed; retained=$retained; preserved=@('.git','.venv','src','tests','docs','private plans','selected design',"$Candidate ZIP",'latest extracted package','validation reports','job histories') }
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $evidenceRoot 'cleanup-result.json') -Encoding utf8
[pscustomobject]@{ beforeMiB=[math]::Round($before/1MB,2); afterMiB=[math]::Round($after/1MB,2); recoveredMiB=[math]::Round(($before-$after)/1MB,2); removed=$removed.Count; retained=$retained } | ConvertTo-Json -Depth 4
