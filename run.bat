@echo off
cd /d "%~dp0"

if not exist ".venv-run\Scripts\python.exe" (
    echo First run - creating virtual environment...
    python -m venv .venv-run
    call .venv-run\Scripts\pip install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright requests
    echo Done!
)

.venv-run\Scripts\python chaoxing_ans.py
pause
