@echo off
echo Starting Daily Job Application Assistant...
cd /d "%~dp0"
venv\Scripts\python.exe src\main.py
echo Pipeline Complete!
pause
