@echo off
chcp 65001 >nul
title duanju-feed publish
cd /d "%~dp0"

python "%~dp0src\generate_feed.py"
if errorlevel 1 (
  echo [FAIL] feed generation failed, abort.
  pause
  exit /b 1
)

git add -A
git diff --cached --quiet && (
  echo [SKIP] nothing to commit.
  exit /b 0
)

git commit -m "chore: refresh duanju.xml"
if errorlevel 1 (
  echo [FAIL] commit failed.
  pause
  exit /b 1
)

git push
if errorlevel 1 (
  echo [FAIL] push failed. Check remote / credentials.
  pause
  exit /b 1
)

echo [OK] pushed. Pages will rebuild in about 1 minute.
timeout /t 3 >nul
exit
