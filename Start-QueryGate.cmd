@echo off
setlocal

py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto run_py

python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto run_python

echo Install Python 3.11 or newer and open a new terminal. 1>&2
exit /b 1

:run_py
py -3 "%~dp0start_querygate.py" %*
exit /b %errorlevel%

:run_python
python "%~dp0start_querygate.py" %*
exit /b %errorlevel%
