$scriptDir = "c:\Users\andyc\Claude AC\Claude AC\Python script"
$python   = "c:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe"
$logFile  = Join-Path $scriptDir "bot_log.txt"

Set-Location $scriptDir

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Bot started" | Out-File $logFile -Encoding UTF8

& $python "$scriptDir\alpaca_trading_bot.py" --live 2>&1 | Tee-Object -FilePath $logFile -Append

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Bot finished" | Out-File $logFile -Encoding UTF8 -Append
