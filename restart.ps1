# 快速重启 uvicorn 服务
$port = 8000
$pid = (netstat -ano | Select-String ":$port ") | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1
if ($pid -and $pid -ne '0') {
    Write-Host "停止进程 PID $pid ..."
    taskkill /F /PID $pid 2>nul
    Start-Sleep -Seconds 1
}
Write-Host "启动服务..."
Start-Process -FilePath "D:\Python3.12.10\python.exe" `
    -ArgumentList "-m uvicorn api.main:app --reload --host 0.0.0.0 --port $port" `
    -WorkingDirectory $PSScriptRoot
Start-Sleep -Seconds 6
try {
    $r = Invoke-RestMethod "http://localhost:$port/health"
    Write-Host "服务已就绪: $($r.status)  环境: $($r.environment)" -ForegroundColor Green
} catch {
    Write-Host "服务启动中，请稍候再刷新浏览器" -ForegroundColor Yellow
}
