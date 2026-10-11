$ErrorActionPreference = "Stop"

$downloadUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$checksumUrl = "$downloadUrl.sha256"
$destination = Join-Path $PSScriptRoot "tools\ffmpeg\bin"
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("jg2bh_ffmpeg_" + [Guid]::NewGuid().ToString("N"))
$archivePath = Join-Path $temporaryRoot "ffmpeg-release-essentials.zip"
$checksumPath = Join-Path $temporaryRoot "ffmpeg-release-essentials.zip.sha256"
$extractPath = Join-Path $temporaryRoot "extracted"

try {
    Write-Host "Downloading the official FFmpeg Windows essentials build..."
    New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePath -UseBasicParsing
    Invoke-WebRequest -Uri $checksumUrl -OutFile $checksumPath -UseBasicParsing

    Write-Host "Verifying the publisher's SHA-256 checksum..."
    $expectedHash = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split "\s+")[0].ToUpperInvariant()
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($expectedHash -notmatch "^[A-F0-9]{64}$" -or $actualHash -ne $expectedHash) {
        throw "FFmpeg checksum verification failed. The downloaded archive was not installed."
    }

    Write-Host "Extracting FFmpeg inside this project..."
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force
    $ffmpeg = Get-ChildItem -LiteralPath $extractPath -Filter "ffmpeg.exe" -File -Recurse | Select-Object -First 1
    $ffprobe = Get-ChildItem -LiteralPath $extractPath -Filter "ffprobe.exe" -File -Recurse | Select-Object -First 1
    if (-not $ffmpeg -or -not $ffprobe) {
        throw "The verified FFmpeg archive did not contain ffmpeg.exe and ffprobe.exe."
    }

    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Copy-Item -LiteralPath $ffmpeg.FullName -Destination (Join-Path $destination "ffmpeg.exe") -Force
    Copy-Item -LiteralPath $ffprobe.FullName -Destination (Join-Path $destination "ffprobe.exe") -Force
    Write-Host "FFmpeg installed successfully: $destination" -ForegroundColor Green
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
