# 当前固件：ESP32 HFP → USB UART0

本目录源于 [5iop/esp32-hfp-bridge](https://github.com/5iop/esp32-hfp-bridge)，已针对本项目修改。请使用根目录的 [使用指南](../docs/使用指南.md)，不要套用上游 UART2 / 外置 I2S DAC 的接线说明。

- ESP32 为 HFP Hands-Free 客户端，手机为 Audio Gateway。
- 双向通话音频及状态走板载 UART0 / USB，921600 波特率。
- 支持 CVSD 8 kHz 和 mSBC 16 kHz 协商；真实电话已验证 16 kHz。
- 外置音频关闭，OLED 不参与通话。
- 增加拨号、挂断、状态、实际音频字节统计与主动重连。
- 运行协议 `firmware=1`，重连版本增加 `bt_build=2`、`bt_auto`、`bt_retry`。

## 构建

```sh
PLATFORMIO_CORE_DIR="$PWD/.pio-core" .venv/bin/pio run -d firmware
```

项目配置使用 `espressif32@6.12.0`、`esp32dev`、ESP-IDF 与 4 MB 分区表。仅实测经典 ESP32-D0WDQ6-V3，其他芯片需要先确认具备经典蓝牙 HFP。

主要实现位于 `main/`；`tools/` 保留上游诊断工具，其部分默认串口、波特率和硬件说明属于上游配置，使用前需核对，不能与正在运行的通话服务同时打开串口。

固件保留原始版权与 `Unlicense OR CC0-1.0` 标记，本项目新增固件代码采用 Unlicense。未标明许可的其他上游文件不在根目录 MIT 授权范围内，详见 [NOTICE](../NOTICE.md)。ESP-IDF 等外部依赖遵循各自许可证。
