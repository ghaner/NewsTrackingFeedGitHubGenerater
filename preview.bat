@echo off
chcp 65001 >nul
title news-feed preview
python "%~dp0src\generate_feed.py"
if errorlevel 1 pause
start "" "%~dp0index.html"
exit
