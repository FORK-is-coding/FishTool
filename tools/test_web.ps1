# PowerShell脚本 - 测试Web服务启动
$ErrorActionPreference = "Continue"

$exePath = "D:\tasks\cola\bili_ops_toolbox\dist\FishTool.exe"

Write-Host "=" -NoNewline
Write-Host ("=" * 59)
Write-Host "测试 FishTool Web 服务启动"
Write-Host "=" -NoNewline
Write-Host ("=" * 59)

# 检查EXE
if (-not (Test-Path $exePath)) {
    Write-Host "❌ EXE不存在: $exePath"
    exit 1
}

$exeInfo = Get-Item $exePath
Write-Host "✅ EXE存在"
Write-Host "   路径: $exePath"
Write-Host "   大小: $([math]::Round($exeInfo.Length / 1MB, 2)) MB"
Write-Host "   修改: $($exeInfo.LastWriteTime)"

# 启动EXE
Write-Host "`n启动桌面模式（会自动拉起Web服务）..."
$process = Start-Process -FilePath $exePath -PassThru -WindowStyle Normal

Write-Host "进程 PID: $($process.Id)"
Write-Host "轮询探测 http://localhost:8000 (上限60秒)...`n"

# 轮询探测
$startTime = Get-Date
$timeout = 60
$success = $false

for ($i = 1; $i -le 30; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8000" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        
        $elapsed = ((Get-Date) - $startTime).TotalSeconds
        
        Write-Host "`n" -NoNewline
        Write-Host ("=" * 60)
        Write-Host "✅ Web服务就绪！耗时 $([math]::Round($elapsed, 1)) 秒"
        Write-Host ("=" * 60)
        Write-Host "响应码: $($response.StatusCode)"
        Write-Host "Content-Type: $($response.Headers['Content-Type'])"
        Write-Host "响应长度: $($response.Content.Length) bytes"
        
        $success = $true
        break
        
    } catch {
        Write-Host "[$i/30] 等待中... ($($_.Exception.GetType().Name))"
        Start-Sleep -Seconds 2
    }
}

# 终止进程
try {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
} catch {
    # 忽略错误
}

if (-not $success) {
    Write-Host "`n" -NoNewline
    Write-Host ("=" * 60)
    Write-Host "❌ Web服务未能在60秒内启动"
    Write-Host ("=" * 60)
    exit 1
}

exit 0
