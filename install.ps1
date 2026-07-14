[CmdletBinding()]
param(
    [string]$InstallDir = $env:ZHIARCHIVE_INSTALL_DIR,
    [string]$Repository = $env:ZHIARCHIVE_REPOSITORY,
    [string]$Ref = $env:ZHIARCHIVE_REF,
    [switch]$Gitee,
    [switch]$ChinaMirror,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$ImageName = "zhi-archive:latest"
$Dockerfile = if ($ChinaMirror) { "CN.Dockerfile" } else { "Dockerfile" }
if (-not $Repository) {
    $Repository = if ($Gitee) {
        "https://gitee.com/amchii/ZhiArchive.git"
    }
    else {
        "https://github.com/amchii/ZhiArchive.git"
    }
}

# 输出普通安装进度。
function Write-InstallLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host "[ZhiArchive] $Message"
}

# 检查必需命令是否存在。
function Assert-Command {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$InstallHint
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "未找到 $Name。$InstallHint"
    }
}

# 判断目录是否为可部署的 ZhiArchive 仓库。
function Test-ProjectDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (
        (Test-Path (Join-Path $Path "archive/config.py") -PathType Leaf) -and
        (Test-Path (Join-Path $Path "pyproject.toml") -PathType Leaf) -and
        (Test-Path (Join-Path $Path "docker-compose.yaml") -PathType Leaf) -and
        (Test-Path (Join-Path $Path "Dockerfile") -PathType Leaf)
    )
}

# 执行原生命令，并在失败时给出一致的错误。
function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

# 生成供应用签名使用的随机密钥。
function New-SecretKey {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return ([BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
}

# 创建运行目录和最小本地配置，保留用户已有内容。
function Initialize-Project {
    param([Parameter(Mandatory = $true)][string]$ProjectDir)

    foreach ($directoryName in @("logs", "results", "states")) {
        $directory = Join-Path $ProjectDir $directoryName
        if (-not (Test-Path $directory)) {
            New-Item -ItemType Directory -Path $directory | Out-Null
        }
    }

    $envFile = Join-Path $ProjectDir ".env"
    if (-not (Test-Path $envFile)) {
        $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
        $content = "secret_key=$(New-SecretKey)`n"
        [IO.File]::WriteAllText($envFile, $content, $utf8WithoutBom)
        Write-InstallLog "已生成 .env 和随机 secret_key"
    }
    else {
        Write-InstallLog "检测到已有 .env，保持不变"
    }
}

# 在远程执行时 clone 仓库，仓库内执行时复用当前工作区。
function Resolve-ProjectDirectory {
    $currentDir = (Get-Location).Path

    if ($PSScriptRoot -and (Test-ProjectDirectory $PSScriptRoot)) {
        return (Resolve-Path $PSScriptRoot).Path
    }
    if (Test-ProjectDirectory $currentDir) {
        return (Resolve-Path $currentDir).Path
    }

    $targetDir = $InstallDir
    if (-not $targetDir) {
        $targetDir = Join-Path $currentDir "ZhiArchive"
    }
    elseif (-not [IO.Path]::IsPathRooted($targetDir)) {
        $targetDir = Join-Path $currentDir $targetDir
    }
    $targetDir = [IO.Path]::GetFullPath($targetDir)

    if (Test-ProjectDirectory $targetDir) {
        return (Resolve-Path $targetDir).Path
    }
    if ((Test-Path $targetDir) -and (Get-ChildItem -Force $targetDir | Select-Object -First 1)) {
        throw "目标目录已存在且不是 ZhiArchive 仓库：$targetDir"
    }

    Write-InstallLog "正在 clone $Repository 到 $targetDir"
    $cloneArguments = @("clone", "--depth", "1")
    if ($Ref) {
        $cloneArguments += @("--branch", $Ref)
    }
    $cloneArguments += @($Repository, $targetDir)
    & git @cloneArguments
    if ($LASTEXITCODE -ne 0) {
        throw "clone 失败，请检查仓库地址和网络。"
    }

    return (Resolve-Path $targetDir).Path
}

try {
    Assert-Command "git" "请先安装 Git for Windows。"
    Assert-Command "docker" "请先安装并启动 Docker Desktop（Linux 容器模式）。"

    if ($Gitee -and -not $ChinaMirror) {
        Write-InstallLog "提示：当前仅从 Gitee clone，构建仍使用默认源；国内构建请同时传入 -ChinaMirror"
    }

    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "当前 Docker 未提供 Compose v2，请更新 Docker Desktop。"
    }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "无法连接 Docker daemon，请确认 Docker Desktop 已启动。"
    }

    $projectDir = Resolve-ProjectDirectory
    if (-not (Test-ProjectDirectory $projectDir)) {
        throw "项目文件不完整：$projectDir"
    }
    if (-not (Test-Path (Join-Path $projectDir $Dockerfile) -PathType Leaf)) {
        throw "未找到 $Dockerfile"
    }

    Initialize-Project $projectDir

    Write-InstallLog "正在使用 $Dockerfile 构建 $ImageName"
    Push-Location $projectDir
    try {
        Invoke-NativeCommand {
            & docker build --tag $ImageName --file $Dockerfile .
        } "Docker 镜像构建失败。"

        if (-not $NoStart) {
            Write-InstallLog "正在启动 API、Redis 和 workers"
            Invoke-NativeCommand {
                & docker compose --file docker-compose.yaml up -d
            } "服务启动失败。"
            Invoke-NativeCommand {
                & docker compose --file docker-compose.yaml ps
            } "无法读取服务状态。"

            Write-InstallLog "安装完成：$projectDir"
            Write-InstallLog "控制台：http://127.0.0.1:9090/zhi/core/config"
        }
        else {
            Write-InstallLog "初始化和镜像构建完成：$projectDir"
            Write-InstallLog "稍后可在该目录运行：docker compose up -d"
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    [Console]::Error.WriteLine("[ZhiArchive] 错误：$($_.Exception.Message)")
    exit 1
}
