@echo off
rem Windows agent launcher: restarts the agent if it ever exits. Started at logon by a scheduled task:
rem   setx POWERLINE_AGENT_KEY "same-secret-as-the-coordinator"
rem   schtasks /Create /TN "Powerline agent" /SC ONLOGON /RL LIMITED /F /TR "\"%USERPROFILE%\powerline-bot\start-agent.cmd\""
cd /d "%~dp0"
:loop
python -u agent.py --coordinator 192.168.1.96 --headless >> agent.log 2>&1
timeout /t 10 /nobreak >nul
goto loop
