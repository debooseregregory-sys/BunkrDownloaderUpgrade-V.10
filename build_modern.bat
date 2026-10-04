@echo off
setlocal
echo Building the independent Bunkr Downloader Modern portable executable...
if not exist venv\Scripts\python.exe (
    py -3 -m venv venv
    if errorlevel 1 exit /b 1
)
call venv\Scripts\activate.bat
python -m pip install -r requirements.txt customtkinter pyinstaller
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean BunkrDownloaderPortableGUI_Modern.spec
if errorlevel 1 exit /b 1
echo Built dist\BunkrDownloaderPortableGUI_Modern.exe
endlocal
