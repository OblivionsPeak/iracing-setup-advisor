@echo off
echo Setting up iRacing Setup Advisor...
py -3 -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
echo.
echo Done! Run run.bat to start the app.
pause
