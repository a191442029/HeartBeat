@echo off
REM ============================================================
REM  HRMLink NUC - OpenSSH Server setup + deploy key (one click)
REM  Double-click on the NUC. UAC popup -> click Yes.
REM  After "ALL DONE", Trae can SSH in with key auth (no password
REM  typed anywhere). Safe to re-run (dedup included).
REM ============================================================
net session >nul 2>&1 || (
  echo Requesting administrator rights, please click Yes on UAC...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo [1/7] Installing OpenSSH Server (needs internet, ~1 min)...
powershell -NoProfile -Command "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"

echo [2/7] Starting sshd and setting autostart...
powershell -NoProfile -Command "Start-Service sshd; Set-Service sshd -StartupType Automatic"

echo [3/7] Adding firewall rule for port 22...
powershell -NoProfile -Command "New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null"

echo [4/7] Setting default shell to PowerShell...
powershell -NoProfile -Command "New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force | Out-Null"

echo [5/7] Allowing blank-password remote logon (no system password needed)...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Lsa" /v LimitBlankPasswordUse /t REG_DWORD /d 0 /f

echo [6/7] Authorizing deploy key (admins + current user)...
if not exist "%ProgramData%\ssh" mkdir "%ProgramData%\ssh"
findstr /C:"AAAAC3NzaC1lZDI1NTE5AAAAIKOrFdriFDZi" "%ProgramData%\ssh\administrators_authorized_keys" >nul 2>&1 || echo ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKOrFdriFDZi/8LbApHEAGAxHNAOMaOqRcTw7TjYxTLJ trae-deploy>>"%ProgramData%\ssh\administrators_authorized_keys"
icacls "%ProgramData%\ssh\administrators_authorized_keys" /inheritance:r /grant "SYSTEM:F" /grant "BUILTIN\Administrators:F" >nul 2>&1
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
findstr /C:"AAAAC3NzaC1lZDI1NTE5AAAAIKOrFdriFDZi" "%USERPROFILE%\.ssh\authorized_keys" >nul 2>&1 || echo ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKOrFdriFDZi/8LbApHEAGAxHNAOMaOqRcTw7TjYxTLJ trae-deploy>>"%USERPROFILE%\.ssh\authorized_keys"

echo [7/7] Result:
powershell -NoProfile -Command "Get-Service sshd | Format-Table -AutoSize Name, Status, StartType"
echo ================== ALL DONE ==================
echo If Status = Running, Trae can now connect.
echo (Close this window, go back to Trae and say: done)
pause
