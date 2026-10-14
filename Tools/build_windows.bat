@echo off
REM One-shot Windows build of FT-Patcher.exe (needs a python.org Python 3.9-3.13
REM install with Tcl/Tk; 3.14 "pythoncore" installs keep Tcl in a zipfs the
REM PyInstaller hook cannot bundle, so they are rejected).
REM Result: dist\FT-Patcher\FT-Patcher.exe  -- share the whole FT-Patcher folder.
setlocal
set "PY="
for %%V in (3.13 3.12 3.11 3.10 3.9) do (
    if not defined PY (
        py -%%V -c "import tkinter,sys; t=tkinter.Tcl(); sys.exit(1 if 'zipfs' in t.eval('info library') else 0)" >nul 2>nul
        if not errorlevel 1 set "PY=py -%%V"
    )
)
if not defined PY (
    py -3 -c "import tkinter,sys; t=tkinter.Tcl(); sys.exit(1 if 'zipfs' in t.eval('info library') else 0)" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    python -c "import tkinter,sys; t=tkinter.Tcl(); sys.exit(1 if 'zipfs' in t.eval('info library') else 0)" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo No suitable Python found (need python.org Python 3.9-3.13 with Tcl/Tk).
    pause
    exit /b 1
)
%PY% -m pip install --upgrade pyinstaller
set "FT_SPEC_DIR=%~dp0"
rmdir /s /q "%~dp0build" 2>nul
rmdir /s /q "%~dp0dist" 2>nul
%PY% -m PyInstaller "%~dp0ft_patcher.spec" --noconfirm --clean
if errorlevel 1 (
    echo.
    echo BUILD FAILED -- see output above.
    exit /b 1
)
echo.
echo Build OK: dist\FT-Patcher\  (zip that folder to share)
echo Note: antivirus may flag PyInstaller exes; add an exclusion or share the folder.
pause