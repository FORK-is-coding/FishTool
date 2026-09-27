# PowerShell 脚本：运行打包测试
# 用于 V7 修复验证

Set-Location "D:\tasks\cola\bili_ops_toolbox"

Write-Host "===== 开始运行打包测试 =====" -ForegroundColor Cyan
Write-Host "时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray
Write-Host ""

# 执行打包脚本
python run_packaging.py

# 检查返回码
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "===== 打包测试完成：SUCCESS =====" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "===== 打包测试完成：FAILED =====" -ForegroundColor Red
}

Write-Host ""
Write-Host "查看报告: PACKAGE_REPORT.md" -ForegroundColor Yellow
Write-Host ""

# 显示报告内容
if (Test-Path "PACKAGE_REPORT.md") {
    Get-Content "PACKAGE_REPORT.md"
}
