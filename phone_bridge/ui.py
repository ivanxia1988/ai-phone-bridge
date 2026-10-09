"""Local phone console with per-provider credentials in macOS Keychain."""
import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from serial.tools import list_ports
from .credentials import MacKeychain, PROVIDERS, ENV_KEYS, validate_provider, validate_key
from .realtime import Realtime
from .doubao import DoubaoRealtime, validate_voice
from .protocol import validate_number
from .service import ROOT, safe_error

WEB = Path(__file__).parent / 'web'
DEFAULT_VOICE = 'zh_female_vv_jupiter_bigtts'
MEDIA = {'remote.wav', 'agent.wav', 'summary.md', 'result.json', 'transcripts.jsonl', 'recording-transcripts.json'}


def api_error_message(exc):
    match = re.search(r'(doubao_error_(?:http_\d{3}|\d+|unknown)|TimeoutError)$', str(exc))
    code = match.group(1) if match else safe_error(exc)
    messages = {
        'doubao_error_http_401': 'API Key 验证失败，请检查是否使用豆包语音控制台的 Key',
        'doubao_error_http_403': '账户或音色权限不足，请检查语音服务与音色授权',
        'TimeoutError': '连接超时，请检查网络后重试',
    }
    return messages.get(code, '音色会话验证失败，请检查 Key、音色 ID 与服务状态（' + code + '）')


class ConsoleController:
    def __init__(self, root=ROOT, ports_fn=None, rpc_call=None, credential_store=None):
        self.root = Path(root)
        self.ports_fn = ports_fn or list_ports.comports
        self.rpc_call = rpc_call or self._request
        self.lock = threading.Lock()
        self.store = credential_store or MacKeychain(self.root)
        self.provider = 'doubao'
        self.credential_error = ''
        self.api_key = ''
        self.voice_id, self.voice_name = DEFAULT_VOICE, 'VV'
        self.verified_voice = None
        self.api_error = ''
        self.preferences = self.root / 'work' / 'ui-preferences.json'
        try:
            data = json.loads(self.preferences.read_text())
            self.provider = validate_provider(data.get('provider', 'doubao'))
            self.voice_id = validate_voice(data['voice_id'])
            self.voice_name = str(data.get('voice_name', '自定义音色'))[:80]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.api_key = self.load_key(self.provider)

    def load_key(self, provider):
        try:
            key = self.store.get(provider)
            self.credential_error = ''
            return key or os.getenv(ENV_KEYS[provider], '')
        except RuntimeError as exc:
            self.credential_error = str(exc)
            return os.getenv(ENV_KEYS[provider], '')

    def key_saved(self, provider):
        try:
            return self.store.has(provider)
        except RuntimeError as exc:
            self.credential_error = str(exc)
            return False

    def _request(self, payload):
        async def run():
            reader, writer = await asyncio.open_unix_connection(str(self.root / 'work' / 'phone.sock'), limit=1024 * 1024)
            try:
                writer.write((json.dumps(payload, ensure_ascii=False) + '\n').encode())
                await writer.drain()
                response = json.loads(await asyncio.wait_for(reader.readline(), 60 if payload['op'] == 'check-api' else 20))
                if not response['ok']:
                    raise RuntimeError(response['error'])
                return response['result']
            finally:
                writer.close()
                await writer.wait_closed()
        return asyncio.run(run())

    def _backend(self):
        try:
            return self.rpc_call({'op': 'status', 'refresh': False}) or {}
        except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
            return {}

    def ports(self):
        candidates = []
        for port in self.ports_fn():
            if isinstance(port, str):
                candidates.append(port)
            elif (port.vid, port.pid) == (0x10C4, 0xEA60):
                candidates.append(port.device)
        preferred = [p for p in candidates if 'SLAB_USBtoUART' in p]
        return preferred or sorted(set(candidates))

    def status(self):
        ports = self.ports()
        backend = self._backend()
        usb = bool(ports)
        firmware = usb and backend.get('port') in ports and backend.get('firmware') == 1 and not backend.get('transport_lost') and backend.get('firmware_alive', True)
        phone = bool(firmware and backend.get('phone_connected'))
        busy = bool(backend.get('current_call') or backend.get('call_active') or backend.get('call_setup') or backend.get('reconnecting'))
        credentials = bool(self.api_key or backend.get('voice_provider') == self.provider and backend.get('voice_credentials_available'))
        api_ready = bool(credentials and self.verified_voice == self.voice_id)
        provider_matches = backend.get('voice_provider') == self.provider
        saved = self.key_saved(self.provider)
        return {'ports': ports, 'usb_connected': usb, 'firmware_ready': bool(firmware),
                'phone_connected': phone, 'credentials_available': credentials, 'api_ready': api_ready,
                'api_error': self.api_error, 'voice_id': self.voice_id, 'voice_name': self.voice_name,
                'busy': busy, 'checking': self.lock.locked(), 'ready': bool(phone and api_ready and provider_matches and not busy),
                'backend': backend, 'current_call': backend.get('current_call'),
                'provider': self.provider, 'provider_name': PROVIDERS[self.provider],
                'credential_saved': saved, 'credential_error': self.credential_error,
                'providers': [{'id': p, 'name': n, 'saved': self.key_saved(p)} for p, n in PROVIDERS.items()]}

    def configure(self, payload):
        backend = self._backend()
        if backend.get('current_call') or backend.get('call_active') or backend.get('call_setup') or backend.get('reconnecting'):
            raise RuntimeError('通话正在处理，请结束后修改设置')
        provider = validate_provider(payload.get('provider', self.provider))
        voice = validate_voice(payload.get('voice_id', self.voice_id))
        name = payload.get('voice_name', self.voice_name)
        if not isinstance(name, str):
            raise ValueError('请填写音色名称')
        key = payload.get('api_key', '')
        if not isinstance(key, str):
            raise ValueError('请输入有效的 API Key')
        key = validate_key(key) if key.strip() else ''
        if key:
            self.store.set(provider, key)
        new_key = key or (self.load_key(provider) if provider != self.provider else self.api_key or self.load_key(provider))
        if provider != self.provider or voice != self.voice_id or new_key != self.api_key:
            self.verified_voice = None
            self.api_error = ''
        self.provider, self.api_key = provider, new_key
        self.voice_id, self.voice_name = voice, name[:80]
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        self.preferences.write_text(json.dumps({'provider': provider, 'voice_id': voice, 'voice_name': self.voice_name}, ensure_ascii=False))
        return {'saved': True, 'credential_saved': self.key_saved(provider), 'provider': provider, 'voice_id': voice}

    def start_backend(self, port):
        environment = os.environ.copy()
        environment.update(PHONE_VOICE_PROVIDER=self.provider, DOUBAO_VOICE=self.voice_id)
        environment[ENV_KEYS[self.provider]] = self.api_key
        log_path = self.root / 'work' / 'service.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'phone_bridge', '--port', port, 'serve'],
                cwd=self.root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('设备服务启动失败，请检查 USB 连接或关闭其他串口工具')
            backend = self._backend()
            if backend:
                return backend
            time.sleep(0.1)
        raise RuntimeError('设备未响应，请重新连接 USB 后检测')

    def connect(self, payload):
        self.configure(payload)
        ports = self.ports()
        backend = self._backend()
        if backend.get('current_call') or backend.get('call_active') or backend.get('call_setup'):
            raise RuntimeError('手机正在通话，请结束后再检测')
        if not self.api_key and not (backend.get('voice_provider') == self.provider and backend.get('voice_credentials_available')):
            raise RuntimeError('请先为' + PROVIDERS[self.provider] + '保存 API Key')
        if ports:
            selected = payload.get('port') or ports[0]
            if selected not in ports:
                raise ValueError('请选择当前连接的设备')
            if backend and backend.get('service_version', 0) < 4 and self.api_key:
                self.rpc_call({'op': 'stop'})
                for _ in range(100):
                    if not (self.root / 'work' / 'phone.sock').exists():
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError('旧服务尚未退出，请稍后重试')
                backend = {}
            if not backend:
                if not self.api_key:
                    raise RuntimeError('请先为该渠道保存 API Key')
                backend = self.start_backend(selected)
            if backend.get('service_version', 0) >= 3:
                configuration = {'op': 'configure', 'provider': self.provider}
                if self.api_key:
                    configuration['api_key'] = self.api_key
                self.rpc_call(configuration)
            elif self.provider != backend.get('voice_provider'):
                raise RuntimeError('请先保存 Key，再检测以升级旧通话服务')
            if backend.get('transport_lost') or backend.get('port') != selected or backend.get('firmware') != 1:
                self.rpc_call({'op': 'reconnect', 'port': selected})
            else:
                self.rpc_call({'op': 'status'})
        self.verified_voice = None
        self.api_error = ''
        try:
            if ports or backend.get('voice_provider') == self.provider and backend.get('voice_credentials_available'):
                self.rpc_call(self.model_request('check-api'))
            else:
                async def verify():
                    model = (DoubaoRealtime('只验证连接，不拨号。', self.api_key, api_key=self.api_key, voice_id=self.voice_id)
                             if self.provider == 'doubao' else Realtime('只验证连接，不拨号。', self.api_key))
                    try:
                        await asyncio.wait_for(model.open(), 45)
                    finally:
                        await model.close()
                asyncio.run(verify())
            self.verified_voice = self.voice_id
        except Exception as exc:
            self.api_error = api_error_message(exc)
            raise RuntimeError(self.api_error) from None
        return self.status()

    def model_request(self, operation):
        request = {'op': operation, 'provider': self.provider}
        if self.provider == 'doubao':
            request['voice_id'] = self.voice_id
        return request

    def call(self, payload):
        if not self.status()['ready']:
            raise RuntimeError('请先完成设备、蓝牙和音色检测，确认手机空闲')
        number = validate_number(payload.get('number', ''))
        task = payload.get('task', '')
        limit = payload.get('max_seconds', 180)
        if not isinstance(task, str) or not 1 <= len(task.strip()) <= 4000:
            raise ValueError('请填写 1–4000 字的沟通目的')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 10 <= limit <= 600:
            raise ValueError('通话时限必须为 10–600 秒')
        request = self.model_request('call')
        request.update(number=number, task=task.strip(), max_seconds=limit)
        return self.rpc_call(request)

    def call_path(self, call_id):
        try:
            if str(uuid.UUID(call_id)) != call_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('无效的通话编号') from None
        base = (self.root / 'calls').resolve()
        path = base / call_id
        if path.is_symlink() or path.resolve().parent != base:
            raise ValueError('无效的通话目录')
        return path

    def calls(self):
        data = []
        base = self.root / 'calls'
        if base.exists():
            for path in base.iterdir():
                try:
                    result = json.loads(self.media_path(path.name, 'result.json').read_text())
                    data.append(result)
                except (OSError, ValueError, TypeError):
                    continue
        return sorted(data, key=lambda r: r.get('created_at', ''), reverse=True)[:100]

    def call_detail(self, call_id):
        path = self.call_path(call_id)
        result = json.loads(self.media_path(call_id, 'result.json').read_text())
        live = self._backend().get('current_call')
        if live and live.get('id') == call_id:
            result = live
        summary = ''
        if (path / 'summary.md').exists():
            summary = self.media_path(call_id, 'summary.md').read_text(encoding='utf-8')
        transcripts = []
        if (path / 'transcripts.jsonl').exists():
            for line in self.media_path(call_id, 'transcripts.jsonl').read_text(encoding='utf-8').splitlines():
                try:
                    transcripts.append(json.loads(line))
                except ValueError:
                    continue
        files = [name for name in sorted(MEDIA) if (path / name).is_file() and not (path / name).is_symlink()]
        return {'result': result, 'summary': summary, 'transcripts': transcripts[-500:], 'files': files}

    def media_path(self, call_id, name):
        if name not in MEDIA:
            raise ValueError('不支持的文件')
        path = self.call_path(call_id) / name
        if path.is_symlink() or path.resolve().parent != self.call_path(call_id).resolve():
            raise ValueError('无效的文件路径')
        return path


def make_server(controller, port=8787):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, data, content_type='application/json; charset=utf-8', headers=None):
            if isinstance(data, dict) or isinstance(data, list):
                data = json.dumps(data, ensure_ascii=False).encode()
            if isinstance(data, str):
                data = data.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def allowed(self, private=True, post=False):
            port = self.server.server_address[1]
            hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            host = self.headers.get('Host', '')
            if host not in hosts or post and self.headers.get('Origin') != 'http://' + host:
                self.send(403, {'error': '仅支持本机控制台访问'})
                return False
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                authenticated = cookie.get('phone_console') and secrets.compare_digest(cookie['phone_console'].value, token)
            except Exception:
                authenticated = False
            if private and not authenticated:
                self.send(403, {'error': '请从本机控制台首页进入'})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.allowed(private=path not in {'/', '/app.js', '/style.css', '/health'}):
                return
            try:
                if path == '/health':
                    self.send(200, {'app': 'esp32-phone-console', 'version': 3})
                elif path in {'/', '/app.js', '/style.css'}:
                    name = 'index.html' if path == '/' else path[1:]
                    mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
                    headers = {'Set-Cookie': f'phone_console={token}; Path=/; HttpOnly; SameSite=Strict'} if path == '/' else {}
                    self.send(200, (WEB / name).read_bytes(), mime, headers)
                elif path == '/api/status':
                    self.send(200, controller.status())
                elif path == '/api/calls':
                    self.send(200, controller.calls())
                elif path.startswith('/api/calls/'):
                    self.send(200, controller.call_detail(path.removeprefix('/api/calls/')))
                elif path.startswith('/media/'):
                    parts = path.split('/')
                    if len(parts) != 4:
                        raise ValueError('无效的文件地址')
                    self.media(controller.media_path(parts[2], parts[3]))
                else:
                    self.send(404, {'error': '页面不存在'})
            except FileNotFoundError:
                self.send(404, {'error': '结果文件尚未生成'})
            except (ValueError, RuntimeError) as exc:
                self.send(400, {'error': str(exc)})
            except Exception:
                self.send(500, {'error': '读取结果失败，请稍后重试'})

        def do_POST(self):
            if not self.allowed(post=True):
                return
            locked = False
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16000 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('请求格式错误或内容过长')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('请求格式错误')
                path = urlsplit(self.path).path
                if path == '/api/hangup':
                    result = controller.rpc_call({'op': 'hangup'})
                else:
                    locked = controller.lock.acquire(blocking=False)
                    if not locked:
                        self.send(409, {'error': '正在处理，请稍候'})
                        return
                    if path == '/api/settings':
                        result = controller.configure(payload)
                    elif path == '/api/connect':
                        result = controller.connect(payload)
                    elif path == '/api/call':
                        result = controller.call(payload)
                    else:
                        self.send(404, {'error': '操作不存在'})
                        return
                self.send(200, result)
            except (ValueError, RuntimeError) as exc:
                self.send(400, {'error': str(exc)})
            except Exception:
                self.send(500, {'error': '操作未完成，请检查设备连接后重试'})
            finally:
                if locked:
                    controller.lock.release()

        def media(self, path):
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            headers = {'Accept-Ranges': 'bytes'}
            range_header = self.headers.get('Range')
            if range_header:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
                if not match or not any(match.groups()):
                    self.send(416, b'', headers={'Content-Range': f'bytes */{size}'})
                    return
                first, last = match.groups()
                start = int(first) if first else max(0, size - int(last))
                end = min(size - 1, int(last)) if first and last else size - 1
                if start >= size or start > end:
                    self.send(416, b'', headers={'Content-Range': f'bytes */{size}'})
                    return
                status = 206
                headers['Content-Range'] = f'bytes {start}-{end}/{size}'
            mime = 'audio/wav' if path.suffix == '.wav' else 'text/plain; charset=utf-8'
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            with path.open('rb') as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='本机 AI 电话控制台')
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args()
    if args.background:
        def running():
            try:
                with urlopen('http://127.0.0.1:8787/health', timeout=1) as response:
                    return json.load(response).get('app') == 'esp32-phone-console'
            except (OSError, ValueError):
                return False
        if running():
            print('电话控制台已在运行：http://127.0.0.1:8787')
            return
        (ROOT / 'work').mkdir(exist_ok=True)
        with (ROOT / 'work' / 'ui.log').open('a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'phone_bridge.ui'], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(50):
            if process.poll() is not None:
                raise SystemExit('控制台启动失败，请检查 8787 端口是否被占用。')
            if running():
                print('电话控制台已启动：http://127.0.0.1:8787')
                return
            time.sleep(0.1)
        raise SystemExit('控制台未就绪，请重新启动。')
    try:
        server = make_server(ConsoleController())
    except OSError:
        raise SystemExit('8787 端口被占用，请检查是否已有控制台运行。')
    print('电话控制台已启动：http://127.0.0.1:8787', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
