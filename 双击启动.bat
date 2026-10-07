@echo off
rem 双击启动 B 站评论管理器（源码部署版，需已安装 Python 并加入 PATH）
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%~dp0bili_comment_gui.py"
) else (
  echo 未找到 pythonw：请先安装 Python 3.9+ 并勾选 Add to PATH，再执行 pip install -r requirements.txt
  pause
)
