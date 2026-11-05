<#
    medlit-agent 安装脚本（Windows PowerShell）

    .\install\install.ps1                    # 自动检测可用的 agent
    .\install\install.ps1 workbuddy          # 指定目标
    .\install\install.ps1 claude-code
    .\install\install.ps1 cursor
    .\install\install.ps1 "D:\some\path\skills"   # 直接给目录

    脚本只做一件事：把 skills\medlit-agent\ 复制到目标 agent 的 skills 目录。
    密钥需要你之后自己填（脚本会提示）。

    若提示脚本被禁止运行，先执行：
      Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>

param([string]$Target = 'auto')

$ErrorActionPreference = 'Stop'
$RepoDir  = Split-Path -Parent $PSScriptRoot
$Src      = Join-Path $RepoDir 'skills\medlit-agent'
$SkillName = 'medlit-agent'

if (-not (Test-Path (Join-Path $Src 'SKILL.md'))) {
    Write-Host "✗ 找不到 $Src\SKILL.md，请在仓库根目录下运行本脚本。" -ForegroundColor Red
    exit 1
}

$candidates = @(
    @{ name = 'workbuddy';   path = "$env:USERPROFILE\.workbuddy\skills" },
    @{ name = 'claude-code'; path = "$env:USERPROFILE\.claude\skills" },
    @{ name = 'codex';       path = "$env:USERPROFILE\.agents\skills" },
    @{ name = 'cursor';      path = "$env:USERPROFILE\.cursor\skills" }
)

switch ($Target) {
    'auto' {
        $dest = $null
        foreach ($c in $candidates) {
            if (Test-Path (Split-Path -Parent $c.path)) { $dest = $c.path; break }
        }
        if (-not $dest) { $dest = $candidates[2].path }
    }
    'workbuddy'   { $dest = $candidates[0].path }
    'claude-code' { $dest = $candidates[1].path }
    'codex'       { $dest = $candidates[2].path }
    'cursor'      { $dest = $candidates[3].path }
    default       { $dest = $Target }
}

New-Item -ItemType Directory -Force -Path $dest | Out-Null
$skillDir = Join-Path $dest $SkillName

if (Test-Path $skillDir) {
    $bak = "$skillDir.bak-$(Get-Date -Format 'yyyyMMdd-HHmm')"
    Write-Host "→ 已存在 $skillDir，先备份为 $(Split-Path -Leaf $bak)"
    Rename-Item $skillDir (Split-Path -Leaf $bak)
}

Copy-Item -Path $Src -Destination $skillDir -Recurse -Force
Write-Host "✓ 已安装到 $skillDir" -ForegroundColor Green

# 生成 .env 模板（不覆盖已有的）
$envFile = Join-Path $skillDir '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $skillDir '.env.example') $envFile
    Write-Host "→ 已生成 .env 模板：$envFile"
}

Write-Host ""
Write-Host "接下来请编辑 $envFile，填入：" -ForegroundColor Cyan
Write-Host "  SILICONFLOW_API_KEY=   （必填，模型服务密钥，例如硅基流动）"
Write-Host "  NCBI_API_KEY=          （选填，有则限速从 3/s 提到 10/s）"
Write-Host "  NCBI_EMAIL=            （建议填你自己的邮箱）"
Write-Host ""
Write-Host "然后自检："
Write-Host "  python `"$skillDir\scripts\selftest.py`""
Write-Host ""
