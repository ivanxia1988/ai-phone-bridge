#!/bin/zsh
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print '未找到项目运行环境，请先完成安装。'
  read '?按回车关闭'
  exit 1
fi
.venv/bin/python -m phone_bridge.ui --background
if [[ $? -ne 0 ]]; then
  read '?按回车关闭'
  exit 1
fi
open 'http://127.0.0.1:8787'
