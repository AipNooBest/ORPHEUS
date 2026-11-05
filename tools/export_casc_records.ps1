#requires -Version 7.0
<#
.SYNOPSIS
Export verified asset key records from a local Overwatch 1.74.0.0.104319 installation.
.DESCRIPTION
Use the built Release/net6.0 directory of OWLib 7afe5f52706e7002ed8c2717299cd60846f3fa3a.
Output is JSON on stdout. Redirect it to local storage outside the source checkout.
This reads assets in memory and verifies content keys and sizes before writing any JSON.
.PARAMETER AssetGuid
Full 16-digit 0x IDs for 003 entity definitions and 00C models.
.PARAMETER IncludeRootModels
Include the direct root model references from the selected entity definitions.
.EXAMPLE
& ./tools/export_casc_records.ps1 -GameRoot $game -DataToolDir $tool -AssetGuid @('0x0400000000000225') -IncludeRootModels
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $GameRoot,
    [Parameter(Mandatory)] [string] $DataToolDir,
    [Parameter(Mandatory, HelpMessage = 'Full 0x GUIDs for 003 definitions or 00C models.')]
    [ValidateNotNullOrEmpty()] [ValidatePattern('^0x(?:0400|0D00)[0-9a-fA-F]{12}$')]
    [string[]] $AssetGuid,
    [switch] $IncludeRootModels
)

$ErrorActionPreference = 'Stop'
$tool = (Resolve-Path -LiteralPath $DataToolDir).Path
foreach ($name in 'K4os.Compression.LZ4', 'ZstdNet', 'DragonLib', 'TACTLib', 'TankLib', 'DataTool') {
    Add-Type -Path (Join-Path $tool "$name.dll")
}
$version = [DataTool.Program].Assembly.GetCustomAttributes(
    [System.Reflection.AssemblyInformationalVersionAttribute], $false
)[0].InformationalVersion
if ($version -notmatch '\+7afe5f52(?:706e7002ed8c2717299cd60846f3fa3a)?$') {
    throw "DataTool build $version does not match the required OWLib revision 7afe5f52."
}

$flags = [DataTool.ToolFlags]::new()
$flags.OverwatchDirectory = (Resolve-Path -LiteralPath $GameRoot).Path
$flags.Language = $flags.SpeechLanguage = 'zhCN'
$flags.NoLanguageRegistry = $true
$flags.UseCache = $flags.CacheCDNData = $flags.TryManifestFallback = $flags.Online = $false
[DataTool.Program]::Flags = $flags
$consoleOut = [Console]::Out
try {
    [Console]::SetOut([IO.TextWriter]::Null)
    [DataTool.Program]::InitStorage($false)
} finally {
    [Console]::SetOut($consoleOut)
}
if ([DataTool.Program]::BuildVersion -ne 104319) {
    throw "Expected build 104319, got $([DataTool.Program]::BuildVersion)."
}

$client = [DataTool.Program]::Client
$handler = [DataTool.Program]::TankHandler
# This pinned TACTLib indexes TRG bundle members internally during InitStorage.
$lookupField = $handler.GetType().GetField(
    'm_hackedBundleLookup', [Reflection.BindingFlags]'NonPublic, Instance'
)
if ($null -eq $lookupField -or $null -eq ($bundles = $lookupField.GetValue($handler))) {
    throw 'The pinned TACTLib TRG bundle index is unavailable.'
}

function Get-ManifestRecord([uint64] $id) {
    if (-not $handler.m_assets.ContainsKey($id)) {
        throw ('Asset 0x{0:X16} is absent from the local asset table.' -f $id)
    }
    $manifest = $handler.GetContentManifestForAsset($id)
    if ($null -eq $manifest -or -not $manifest.Exists($id)) {
        throw ('Asset 0x{0:X16} is absent from its content manifest.' -f $id)
    }
    return $manifest.GetHashData($id)
}

function Assert-Content([IO.Stream] $stream, [long] $size, [string] $ckey, [uint64] $id) {
    if ($null -eq $stream -or $stream.Length -ne $size) {
        throw ('Missing content or size mismatch for 0x{0:X16}.' -f $id)
    }
    $actual = [Convert]::ToHexString([Security.Cryptography.MD5]::HashData($stream))
    if ($actual -ine $ckey) {
        throw ('Content key mismatch for 0x{0:X16}.' -f $id)
    }
    $stream.Position = 0
}

function Open-EncodedRecord([uint64] $id, $content) {
    $entry = [TACTLib.Core.EncodingHandler+CKeyEntry]::new()
    if (-not $client.EncodingHandler.TryGetEncodingEntry($content.ContentKey, [ref] $entry)) {
        throw ('No encoding entry for 0x{0:X16}.' -f $id)
    }
    $ckey = $content.ContentKey.ToHexString().ToLowerInvariant()
    $stream = $client.OpenEKey($entry.EKey)
    try {
        Assert-Content $stream $content.Size $ckey $id
        return @{
            Stream = $stream
            Record = [ordered] @{
                ckey = $ckey
                ekey = $entry.EKey.ToHexString().ToLowerInvariant()
                size = $content.Size
            }
        }
    } catch {
        if ($null -ne $stream) { $stream.Dispose() }
        throw
    }
}

function Open-AssetRecord([uint64] $id) {
    $content = Get-ManifestRecord $id
    [uint64] $container = 0
    if (-not $bundles.TryGetValue($id, [ref] $container)) {
        return (Open-EncodedRecord $id $content)
    }

    $file = Open-EncodedRecord $container (Get-ManifestRecord $container)
    try {
        $bytes = $file.Stream.ToArray()
    } finally {
        $file.Stream.Dispose()
    }
    $bundle = [TACTLib.Core.Product.Tank.Bundle]::new([IO.MemoryStream]::new($bytes, $false), $true)
    $entries = @($bundle.Entries | Where-Object GUID -eq $id)
    if ($entries.Count -ne 1) {
        throw ('Bundle 0x{0:X16} must contain exactly one entry for 0x{1:X16}.' -f $container, $id)
    }
    $offset = [long] $entries[0].Offset
    if ($offset -gt $bytes.Length -or $content.Size -gt ($bytes.Length - $offset)) {
        throw ('Asset 0x{0:X16} extends beyond its bundle.' -f $id)
    }
    $ckey = $content.ContentKey.ToHexString().ToLowerInvariant()
    $stream = [IO.MemoryStream]::new($bytes, [int] $offset, [int] $content.Size, $false)
    try {
        Assert-Content $stream $content.Size $ckey $id
        $file.Record['offset'] = $offset
        return @{
            Stream = $stream
            Record = [ordered] @{ ckey = $ckey; size = $content.Size; bundle = $file.Record }
        }
    } catch {
        $stream.Dispose()
        throw
    }
}

$pending = [Collections.Generic.SortedSet[uint64]]::new()
foreach ($text in $AssetGuid) {
    [void] $pending.Add([Convert]::ToUInt64($text.Substring(2), 16))
}
$records = [ordered] @{}
while ($pending.Count -gt 0) {
    $id = $pending.Min
    [void] $pending.Remove($id)
    $guid = '0x{0:X16}' -f $id
    if ($records.Contains($guid)) { continue }
    $asset = Open-AssetRecord $id
    try {
        if ($IncludeRootModels -and [TankLib.teResourceGUID]::Type($id) -eq 0x003) {
            $stu = [TankLib.STU.teStructuredData]::new($asset.Stream, $true)
            try {
                $definition = $stu.Instances[0]
                if ($definition -isnot [TankLib.STU.Types.STUEntityDefinition]) {
                    throw "Expected STUEntityDefinition for $guid."
                }
                foreach ($component in $definition.m_componentMap.Values) {
                    if ($component -isnot [TankLib.STU.Types.STUModelComponent]) { continue }
                    $model = [uint64] $component.m_model.GUID
                    if ($model -eq 0) { continue }
                    if (($model -shr 48) -ne 0x0D00) {
                        throw "Root model for $guid is not a 00C asset."
                    }
                    [void] $pending.Add($model)
                }
            } finally {
                $stu.Dispose()
            }
        }
        $records[$guid] = $asset.Record
    } finally {
        $asset.Stream.Dispose()
    }
}

[ordered] @{
    schema = 'orpheus-casc-records-v1'
    build = '1.74.0.0.104319'
    records = $records
} | ConvertTo-Json -Depth 6
