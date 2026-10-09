# 来源与许可证范围

## 上游固件

来源：[5iop/esp32-hfp-bridge](https://github.com/5iop/esp32-hfp-bridge)

参考提交：`2366c3c721fa189bef325d31502333e5842bc01b`。

部分固件源文件基于 Espressif HFP client 示例，保留 Espressif 原始版权行及 `Unlicense OR CC0-1.0` SPDX 声明，附 Unlicense 文本于 `firmware/LICENSE`。上游仓库未提供根目录许可证，部分保留文件没有明确的许可标记；本项目不为这些上游文件另行授予 MIT 权利。其他依赖的许可由各自项目保留。

## 本项目补充

Python AI 通话服务、中文控制台、豆包 / OpenAI 适配、Prompt、录音 / 转写 / 总结、测试与新增文档由本项目提供，采用根目录 MIT 许可证。

固件改动包括 UART0 板载 USB 传输、关闭外置音频、拨号与状态回报、主动重连。本项目新增的固件代码采用 Unlicense，带 SPDX 标记的修改文件沿用其原声明。

本项目与豆包、OpenAI、Apple、Espressif 无官方隶属关系。产品名称仅用于说明兼容接口及实际验证设备。
