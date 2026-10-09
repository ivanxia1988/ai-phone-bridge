#!/bin/zsh
cd "${0:A:h}" || exit 1
export DOUBAO_VOICE='zh_female_vv_jupiter_bigtts'
.venv/bin/python -m phone_bridge setup --provider doubao
if (( $? != 0 )); then
  printf '\n启动或 API 检查失败，请按上面的提示检查配置。按回车关闭窗口。\n'
  read -r
  exit 1
fi
printf '\n接下来请在 iPhone「设置 → 蓝牙」连接 ESP32 Phone Bridge。\n'
printf '按回车关闭此启动窗口；后台服务会继续运行。\n'
read -r
