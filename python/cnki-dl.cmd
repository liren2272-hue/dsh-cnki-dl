@echo off
setlocal
set "BSK_AUTO_START=0"
set "PYTHONIOENCODING=utf-8"
"C:\Program Files\Python311\python.exe" "%~dp0run.py" %*
exit /b %ERRORLEVEL%