import http.client
import json
import threading
import uuid

import pytest

from phone_bridge.ui import ConsoleController, make_server


@pytest.fixture(autouse=True)
def isolated_keychain(monkeypatch):
    class Store:
        def __init__(self):
            self.data = {}
        def has(self, provider):
            return provider in self.data
        def get(self, provider):
            return self.data.get(provider, '')
        def set(self, provider, key):
            self.data[provider] = key
    monkeypatch.setattr('phone_bridge.ui.MacKeychain', lambda root: Store())


def fixture_call(root):
    call_id = str(uuid.uuid4())
    path = root / "calls" / call_id
    path.mkdir(parents=True)
    result = {"id": call_id, "state": "completed", "number": "13812345678", "task": "测试",
              "created_at": "2026-10-08T23:04:49+0800", "hangup_confirmed": True,
              "audio_bytes": {"remote": 100, "agent": 80}, "duration_seconds": 10}
    (path / "result.json").write_text(json.dumps(result))
    (path / "summary.md").write_text("真实总结", encoding="utf-8")
    (path / "remote.wav").write_bytes(b"0123456789")
    (path / "transcripts.jsonl").write_text(json.dumps({"role": "remote", "text": "你好", "seconds": 1}))
    return call_id


def test_console_missing_hardware_and_credentials_does_not_dial(tmp_path):
    controller = ConsoleController(root=tmp_path, ports_fn=lambda: [], rpc_call=lambda _: None)
    status = controller.status()
    assert not status["ready"]
    assert not status["usb_connected"]
    with pytest.raises(RuntimeError, match="检测"):
        controller.call({"number": "13812345678", "task": "测试", "max_seconds": 120})


def test_preferences_never_store_or_return_api_key(tmp_path):
    controller = ConsoleController(root=tmp_path, ports_fn=lambda: [], rpc_call=lambda _: None)
    controller.configure({"api_key": "test-only-private-key", "voice_id": "S_test_ivan", "voice_name": "ivan"})
    preferences = (tmp_path / "work" / "ui-preferences.json").read_text()
    assert "S_test_ivan" in preferences
    assert "test-only-private-key" not in preferences
    assert "test-only-private-key" not in json.dumps(controller.status())


def test_call_details_are_real_and_media_paths_are_restricted(tmp_path):
    call_id = fixture_call(tmp_path)
    controller = ConsoleController(root=tmp_path, ports_fn=lambda: [], rpc_call=lambda _: None)
    assert controller.calls()[0]["id"] == call_id
    detail = controller.call_detail(call_id)
    assert detail["summary"] == "真实总结"
    assert detail["transcripts"][0]["text"] == "你好"
    assert controller.media_path(call_id, "remote.wav").read_bytes() == b"0123456789"
    for name in ("../../.env", "../result.json", "secret.txt"):
        with pytest.raises(ValueError):
            controller.media_path(call_id, name)
    with pytest.raises(ValueError):
        controller.call_detail("../../README.md")


def test_http_requires_local_origin_and_serves_audio_ranges(tmp_path):
    call_id = fixture_call(tmp_path)
    controller = ConsoleController(root=tmp_path, ports_fn=lambda: [], rpc_call=lambda _: None)
    server = make_server(controller, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response.read()
        connection.request("POST", "/api/settings", body=b"{}", headers={"Cookie": cookie,
            "Origin": "https://untrusted.example", "Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", "/api/status", headers={"Host": "untrusted.example", "Cookie": cookie})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", f"/media/{call_id}/remote.wav", headers={"Cookie": cookie, "Range": "bytes=2-5"})
        response = connection.getresponse()
        assert response.status == 206
        assert response.getheader("Content-Range") == "bytes 2-5/10"
        assert response.read() == b"2345"
        connection.request("GET", f"/media/{call_id}/remote.wav", headers={"Cookie": cookie, "Range": "bytes=99-"})
        response = connection.getresponse()
        assert response.status == 416
        response.read()
        connection.request("GET", "/api/status", headers={"Cookie": cookie})
        response = connection.getresponse()
        assert response.status == 200
        assert not json.loads(response.read())["ready"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_phone_ringing_blocks_ui_readiness(tmp_path):
    backend = {'port':'/dev/cu.test', 'firmware':1, 'phone_connected':True,
               'voice_provider':'doubao','voice_credentials_available':True,'call_setup':1}
    controller = ConsoleController(root=tmp_path, ports_fn=lambda:['/dev/cu.test'], rpc_call=lambda _:backend)
    controller.verified_voice = controller.voice_id
    assert controller.status()['busy']
    assert not controller.status()['ready']


def test_ready_call_forwards_selected_voice_and_settings_invalidate_verification(tmp_path):
    requests = []
    backend = {'port':'/dev/cu.test','firmware':1,'phone_connected':True,
               'voice_provider':'doubao','voice_credentials_available':True}
    def rpc(payload):
        requests.append(payload)
        return backend if payload['op']=='status' else {'id': 'test-only'}
    controller = ConsoleController(root=tmp_path,ports_fn=lambda:['/dev/cu.test'],rpc_call=rpc)
    controller.verified_voice = controller.voice_id
    assert controller.status()['ready']
    controller.call({'number':'13812345678','task':'测试','max_seconds':120})
    assert requests[-1]['voice_id']=='zh_female_vv_jupiter_bigtts'
    controller.configure({'voice_id':'zh_male_yunzhou_jupiter_bigtts','voice_name':'云舟'})
    assert not controller.status()['api_ready']


def test_media_symlink_cannot_expose_other_local_files(tmp_path):
    call_id = fixture_call(tmp_path)
    secret = tmp_path / 'test-secret'
    secret.write_text('test-only')
    media = tmp_path / 'calls' / call_id / 'remote.wav'
    media.unlink()
    media.symlink_to(secret)
    controller = ConsoleController(root=tmp_path, ports_fn=lambda: [], rpc_call=lambda _: None)
    with pytest.raises(ValueError):
        controller.media_path(call_id,'remote.wav')


def test_wrapped_api_error_preserves_safe_code_without_credentials():
    from phone_bridge.ui import api_error_message
    assert 'Key' in api_error_message(RuntimeError('doubao 实时会话连接失败：doubao_error_http_401'))
    assert '权限' in api_error_message(RuntimeError('doubao 实时会话连接失败：doubao_error_http_403'))
    assert 'test-private' not in api_error_message(RuntimeError('test-private'))
    assert '超时' in api_error_message(RuntimeError('doubao 实时会话连接失败：TimeoutError'))


def test_stale_firmware_prevents_call(tmp_path):
    backend = {'port':'/dev/cu.test','firmware':1,'firmware_alive':False,'phone_connected':True,
               'voice_provider':'doubao','voice_credentials_available':True}
    controller = ConsoleController(root=tmp_path,ports_fn=lambda:['/dev/cu.test'],rpc_call=lambda _:backend)
    controller.verified_voice = controller.voice_id
    assert not controller.status()['firmware_ready']
    assert not controller.status()['ready']
