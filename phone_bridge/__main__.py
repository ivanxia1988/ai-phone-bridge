import argparse
import asyncio
import json
import getpass
import os
import subprocess
import sys
import time

from .service import ROOT, SOCKET, PhoneService
from .transport import detect_port, probe
from .doubao import DoubaoRealtime


async def request(payload):
    reader, writer = await asyncio.open_unix_connection(str(SOCKET))
    writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    result = json.loads(await asyncio.wait_for(reader.readline(), 60 if payload["op"] == "check-api" else 15))
    writer.close()
    await writer.wait_closed()
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["result"]


def main():
    parser = argparse.ArgumentParser(description="通过 ESP32 使用 iPhone 进行 AI 电话")
    parser.add_argument("--port", default=os.getenv("ESP32_PORT"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="查询真实固件并验证二进制回环")
    sub.add_parser("serve", help="前台运行服务")
    sub.add_parser("start", help="后台启动服务，继承当前环境变量")
    setup = sub.add_parser("setup", help="在本机终端安全输入密钥并启动服务")
    setup.add_argument("--provider", choices=("openai", "doubao"), default=os.getenv("PHONE_VOICE_PROVIDER", "doubao"))
    sub.add_parser("check-api", help="验证实时语音模型连接，不拨号")
    sub.add_parser("status")
    call = sub.add_parser("call")
    call.add_argument("--number", required=True)
    call.add_argument("--task", required=True)
    call.add_argument("--voice-id", help="本次豆包通话使用的音色 ID")
    call.add_argument("--max-seconds", type=int, default=180)
    sub.add_parser("hangup")
    sub.add_parser("stop")
    result = sub.add_parser("result")
    result.add_argument("id")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            environment = os.environ.copy()
            environment["PHONE_VOICE_PROVIDER"] = args.provider
            if args.provider == "doubao":
                key = os.getenv("DOUBAO_API_KEY") or getpass.getpass("请输入豆包语音 API Key（不需要 APP ID，不会显示或保存）：")
                key_name = "DOUBAO_API_KEY"
                environment.pop("DOUBAO_APP_ID", None)
                environment.pop("DOUBAO_ACCESS_TOKEN", None)
            else:
                key = os.getenv("OPENAI_API_KEY") or getpass.getpass("请输入 OpenAI API Key（不会显示或保存）：")
                key_name = "OPENAI_API_KEY"
            if not key.strip():
                raise ValueError("密钥不能为空")
            if args.provider == "doubao":
                async def verify_doubao():
                    client = DoubaoRealtime("只验证连接，不拨号。", key.strip(), api_key=key.strip())
                    try:
                        await client.open()
                    finally:
                        await client.close()
                asyncio.run(verify_doubao())
                print("豆包 O2 会话验证成功，准备启动后台服务。")
            if SOCKET.exists():
                status = asyncio.run(request({"op": "status"}))
                if status["current_call"]:
                    raise RuntimeError("当前有通话，暂不重启服务")
                asyncio.run(request({"op": "stop"}))
                for _ in range(50):
                    if not SOCKET.exists():
                        break
                    time.sleep(0.1)
                if SOCKET.exists():
                    raise RuntimeError("旧服务尚未退出")
            environment[key_name] = key.strip()
            command = [sys.executable, "-m", "phone_bridge"]
            if args.port:
                command.extend(["--port", args.port])
            command.append("start")
            completed = subprocess.run(command, cwd=ROOT, env=environment)
            if completed.returncode:
                raise SystemExit(completed.returncode)
            completed = subprocess.run([sys.executable, "-m", "phone_bridge", "check-api"], cwd=ROOT)
            raise SystemExit(completed.returncode)
        elif args.command == "probe":
            data = probe(args.port or detect_port(), int(os.getenv("ESP32_BAUD", "921600")))
        elif args.command == "serve":
            asyncio.run(PhoneService(args.port or detect_port()).serve())
            return
        elif args.command == "start":
            if SOCKET.exists():
                try:
                    data = asyncio.run(request({"op": "status"}))
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                    return
                except (OSError, RuntimeError):
                    pass
            SOCKET.parent.mkdir(parents=True, exist_ok=True)
            with (SOCKET.parent / "service.log").open("a") as log:
                cmd = [sys.executable, "-m", "phone_bridge"]
                if args.port:
                    cmd.extend(["--port", args.port])
                cmd.append("serve")
                process = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=log,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("服务启动失败，请查看 work/service.log")
                if SOCKET.exists():
                    data = asyncio.run(request({"op": "status"}))
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("服务尚未就绪，请查看 work/service.log")
        else:
            payload = {"op": args.command}
            if args.command == "call":
                payload.update(number=args.number, task=args.task, max_seconds=args.max_seconds)
                if args.voice_id:
                    payload["voice_id"] = args.voice_id
            elif args.command == "result":
                payload["id"] = args.id
            data = asyncio.run(request(payload))
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as exc:
        if isinstance(exc, FileNotFoundError) and not SOCKET.exists():
            message = "服务未运行，请先执行 start"
        else:
            message = str(exc)
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
