param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\程序'),
    [string]$BuildPythonPath = (Join-Path $PSScriptRoot '..\..\GatewayCaptureV5\.build-venv\Scripts\python.exe'),
    [string]$AssetsDirectory = ''
)
$ErrorActionPreference = 'Stop'
$version = '1.1.0'
Push-Location $PSScriptRoot
try {
    $buildPython = (Resolve-Path -LiteralPath $BuildPythonPath).Path
    $assetSource = if ([string]::IsNullOrWhiteSpace($AssetsDirectory)) { $null } else { (Resolve-Path -LiteralPath $AssetsDirectory).Path }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

    $parserDist = Join-Path $PSScriptRoot 'build\parser-dist'
    $parserWork = Join-Path $PSScriptRoot 'build\pyinstaller'
    New-Item -ItemType Directory -Path $parserDist,$parserWork -Force | Out-Null
    & $buildPython -m PyInstaller --noconfirm --distpath $parserDist --workpath $parserWork Parser/NvaParser.spec
    if ($LASTEXITCODE -ne 0) { throw 'NvaParser build failed.' }
    $parserExe = Join-Path $parserDist 'NvaParser.exe'
    if (!(Test-Path -LiteralPath $parserExe)) { throw 'NvaParser.exe was not produced.' }

    if ($assetSource) {
        & $buildPython tools/build_asset_pak.py `
            --source $assetSource `
            --pak (Join-Path $OutputDirectory 'NvaAssets.pak') `
            --models (Join-Path $OutputDirectory 'models') `
            --version $version
        if ($LASTEXITCODE -ne 0) { throw 'NvaAssets.pak build failed.' }
    } elseif (!(Test-Path -LiteralPath (Join-Path $OutputDirectory 'NvaAssets.pak')) -or
              !(Test-Path -LiteralPath (Join-Path $OutputDirectory 'models\manifest.json'))) {
        throw 'Pass -AssetsDirectory when publishing to a directory without NvaAssets.pak and models.'
    }

    dotnet publish NvaLocalParser.csproj -c Release -r win-x64 --self-contained true `
        -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true `
        -p:EnableCompressionInSingleFile=true -p:DebugType=None -p:DebugSymbols=false `
        -p:EmbedNvaParser=true "-p:ParserExecutable=$parserExe" -o $OutputDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Nva local parser publish failed.' }

    # 卡密服务地址：随包带一份模板，换域名时直接改输出目录里的 license.json 即可
    $licenseTemplate = Join-Path $PSScriptRoot 'license.json'
    if (Test-Path -LiteralPath $licenseTemplate) {
        Copy-Item -LiteralPath $licenseTemplate -Destination (Join-Path $OutputDirectory 'license.json') -Force
    }

    $sitePackages = Join-Path (Split-Path (Split-Path $buildPython)) 'Lib\site-packages'
    $numpyLicense = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter 'numpy-*.dist-info' |
        ForEach-Object { Join-Path $_.FullName 'licenses\LICENSE.txt' } | Select-Object -First 1
    $pythonLicense = (& $buildPython -c 'import pathlib,sys; print(pathlib.Path(sys.base_prefix) / "LICENSE.txt")').Trim()
    $projectAssets = Get-Content -LiteralPath 'obj\project.assets.json' -Raw | ConvertFrom-Json
    $runtime = $projectAssets.project.frameworks.PSObject.Properties.Value.downloadDependencies |
        Where-Object name -EQ 'Microsoft.NETCore.App.Runtime.win-x64' | Select-Object -First 1
    $runtimeVersion = $runtime.version.TrimStart('[').Split(',')[0]
    $runtimeDir = $projectAssets.packageFolders.PSObject.Properties.Name |
        ForEach-Object { Join-Path $_ "microsoft.netcore.app.runtime.win-x64\$runtimeVersion" } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    $dotnetLicense = Join-Path $runtimeDir 'LICENSE.TXT'
    $dotnetNotices = Join-Path $runtimeDir 'THIRD-PARTY-NOTICES.TXT'
    if (!(Test-Path -LiteralPath $dotnetLicense) -or (Get-Item -LiteralPath $dotnetLicense).Length -le 100) {
        $dotnetLicense = Join-Path $PSScriptRoot 'third-party\DOTNET-LICENSE.TXT'
    }
    if (!(Test-Path -LiteralPath $dotnetNotices) -or (Get-Item -LiteralPath $dotnetNotices).Length -le 100) {
        $dotnetNotices = Join-Path $PSScriptRoot 'third-party\DOTNET-THIRD-PARTY-NOTICES.TXT'
    }
    $noticeSources = @(
        @{ Title = 'WinDivert'; Path = (Join-Path $PSScriptRoot 'dependencies\WinDivert-2.2.2\LICENSE') },
        @{ Title = 'Leaflet'; Path = (Join-Path $PSScriptRoot 'Parser\ErCore\web\vendor\leaflet\LICENSE') },
        @{ Title = 'Three.js'; Path = (Join-Path $PSScriptRoot 'Parser\ErCore\web\vendor\three\LICENSE') },
        @{ Title = 'NumPy'; Path = $numpyLicense },
        @{ Title = 'Python'; Path = $pythonLicense },
        @{ Title = '.NET Runtime License'; Path = $dotnetLicense },
        @{ Title = '.NET Third-Party Notices'; Path = $dotnetNotices }
    )
    $notices = [System.Text.StringBuilder]::new()
    foreach ($item in $noticeSources) {
        if (!(Test-Path -LiteralPath $item.Path)) { throw "Missing notice: $($item.Title)" }
        [void]$notices.AppendLine(('=' * 78))
        [void]$notices.AppendLine($item.Title)
        [void]$notices.AppendLine(('=' * 78))
        [void]$notices.AppendLine([System.IO.File]::ReadAllText($item.Path))
        [void]$notices.AppendLine()
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $OutputDirectory 'THIRD-PARTY-NOTICES.txt'),
        $notices.ToString(),
        [System.Text.UTF8Encoding]::new($false))

    foreach ($obsolete in @('assets','parser','licenses','WinDivert-LICENSE.txt','GatewayAssets.pak','GatewayCapture.exe')) {
        $path = Join-Path $OutputDirectory $obsolete
        if (Test-Path -LiteralPath $path) { throw "Obsolete release entry remains: $path" }
    }
    Write-Host "TQ $version published: $OutputDirectory"
} finally {
    Pop-Location
}
