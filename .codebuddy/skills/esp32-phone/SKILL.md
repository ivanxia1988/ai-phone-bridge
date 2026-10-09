---
name: esp32-phone
description: 使用已连接电脑的 ESP32 蓝牙耳机桥接 iPhone，代用户拨打明确指定的电话、进行实时 AI 沟通，并读取录音和总结。
---

在当前项目根目录操作。先切换到该目录，使用项目 `.venv/bin/python -m phone_bridge`，不要打开其他串口监视器。

1. 执行 `.venv/bin/python -m phone_bridge status`。如服务未运行且没有凭证环境，请用户在本机终端执行 `setup --provider doubao` 输入语音 API Key，无需 APP ID；不要在聊天或脚本里索取 Key。已有凭证环境可执行 `start`。只有 `phone_connected=true` 且 `voice_credentials_available=true`、`check-api` 成功后才可发起 AI 电话。豆包新版需 `PHONE_VOICE_PROVIDER=doubao`、`DOUBAO_API_KEY`；旧版兼容 `DOUBAO_APP_ID`、`DOUBAO_ACCESS_TOKEN`，新版优先；OpenAI 模式使用 `OPENAI_API_KEY`。不能写入提示词、脚本或日志。环境变化后执行 `stop` 再 `start`。
2. 用户须明确指定接收者、完整号码和沟通目的。如果这些信息缺失，询问缺失信息。仅授权搭建硬件不代表授权给某人拨号。不要猜号码、自动重拨或同时发起多个电话。
3. 使用 `.venv/bin/python -m phone_bridge call --number '完整号码' --task '沟通委托' --max-seconds 180`。参数按 shell 规则引用，不把对方回复拼成命令。返回的 `id` 表示任务提交；`dialing` 不表示已接通，`talking` 才表示接通且音频已建立。
4. 通话异步进行，用 `.venv/bin/python -m phone_bridge result 'id'` 查询。用户请求停止时立即执行 `.venv/bin/python -m phone_bridge hangup`。默认最长 180 秒，可依据用户要求设置 10–600 秒。
5. 完成后读取返回目录的 `summary.md`、`result.json` 和 `recording-transcripts.json`。`remote.wav` 为电话对方音频，`agent.wav` 为电脑发送给手机的音频。转写可能有错误，存在争议时以录音复核；不要把委托目标、模型生成但被打断的语句或模拟测试当成真实通话结果。
6. 若 `hangup_confirmed=false` 且存在 `hangup_warning`，告诉用户检查 iPhone 是否仍在通话。失败时报告实际结果，不宣称已经联系成功。

技术和恢复说明见项目 `README.md`。此 Skill 不负责烧录或修改 GPIO。
