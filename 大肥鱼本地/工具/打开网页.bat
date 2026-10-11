@echo off
chcp 936 >nul
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 180;$i++){ try{ $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8080/health' -UseBasicParsing -TimeoutSec 2; if($r.StatusCode -eq 200){ break } }catch{}; Start-Sleep -Milliseconds 1000 }; Start-Process 'http://127.0.0.1:8080'"
exit /b
