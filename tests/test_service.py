import asyncio

import pytest

from phone_bridge.service import PhoneService, safe_error


def test_missing_key_blocks_call_before_any_device_command(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    async def scenario():
        service = PhoneService("fake")
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await service.command({"op": "call", "number": "13812345678", "task": "测试"})
        assert service.current is None
    asyncio.run(scenario())


def test_result_rejects_path_traversal():
    async def scenario():
        service = PhoneService("fake")
        with pytest.raises(ValueError, match="无效"):
            await service.command({"op": "result", "id": "../../README.md"})
    asyncio.run(scenario())


def test_doubao_requires_both_credentials_before_dial(monkeypatch):
    monkeypatch.delenv("DOUBAO_API_KEY", raising=False)
    monkeypatch.setenv("PHONE_VOICE_PROVIDER", "doubao")
    monkeypatch.setenv("OPENAI_API_KEY", "unused-test-key")
    monkeypatch.setenv("DOUBAO_ACCESS_TOKEN", "test-only")
    monkeypatch.delenv("DOUBAO_APP_ID", raising=False)
    async def scenario():
        service = PhoneService("fake")
        assert service.status()["voice_provider"] == "doubao"
        assert not service.status()["voice_credentials_available"]
        with pytest.raises(RuntimeError, match="DOUBAO_APP_ID"):
            await service.command({"op": "call", "number": "13812345678", "task": "测试"})
        assert service.current is None
        monkeypatch.setenv("DOUBAO_APP_ID", "test-app")
        assert service.voice_key() == "test-only"
        assert service.status()["voice_credentials_available"]
    asyncio.run(scenario())


def test_doubao_new_console_needs_only_api_key(monkeypatch):
    monkeypatch.setenv("PHONE_VOICE_PROVIDER", "doubao")
    monkeypatch.setenv("DOUBAO_API_KEY", "new-console-test-key")
    monkeypatch.delenv("DOUBAO_APP_ID", raising=False)
    monkeypatch.delenv("DOUBAO_ACCESS_TOKEN", raising=False)
    service = PhoneService("fake")
    assert service.credentials_available()
    assert service.voice_key() == "new-console-test-key"


def test_known_call_errors_are_diagnostic_without_exposing_network_details():
    assert safe_error(RuntimeError("dial_rejected")) == "dial_rejected"
    assert safe_error(RuntimeError("ended_before_answer")) == "ended_before_answer"
    assert safe_error(RuntimeError("doubao_error_http_401")) == "doubao_error_http_401"
    assert safe_error(RuntimeError("headers contain private credentials")) == "RuntimeError"
    assert safe_error(RuntimeError("doubao_error_secret_credentials")) == "RuntimeError"


def test_status_snapshot_works_when_usb_is_disconnected():
    async def scenario():
        service = PhoneService("fake")
        service.state.transport_lost = True
        data = await service.command({"op": "status", "refresh": False})
        assert data["transport_lost"]
        assert data["service_version"] == 4
    asyncio.run(scenario())


def test_check_api_passes_selected_voice_to_model(monkeypatch):
    monkeypatch.setenv("PHONE_VOICE_PROVIDER", "doubao")
    monkeypatch.setenv("DOUBAO_API_KEY", "test-only")
    created = []
    class Model:
        def __init__(self, task, key, *, voice_id=None):
            created.append(voice_id)
        async def open(self):
            pass
        async def close(self):
            pass
    async def scenario():
        service = PhoneService("fake", realtime_factory=Model)
        result = await service.command({"op": "check-api", "voice_id": "S_test_ivan"})
        assert created == ["S_test_ivan"]
        assert result["voice_id"] == "S_test_ivan"
    asyncio.run(scenario())


def test_reconnect_is_rejected_during_managed_call():
    async def scenario():
        service = PhoneService("fake")
        service.current = object()
        with pytest.raises(RuntimeError, match="通话"):
            await service.command({"op": "reconnect", "port": "fake"})
    asyncio.run(scenario())


def test_call_rechecks_reconnect_after_refresh(monkeypatch):
    monkeypatch.setenv('PHONE_VOICE_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    async def scenario():
        service = PhoneService('fake')
        async def refresh():
            service.reconnecting = True
        service.refresh = refresh
        with pytest.raises(RuntimeError, match='通话'):
            await service.command({'op':'call','number':'13812345678','task':'测试'})
        assert service.current is None
    asyncio.run(scenario())


def test_stale_firmware_snapshot_is_not_healthy():
    import time
    service = PhoneService('fake')
    service.state.firmware = 1
    service.last_state = time.monotonic() - 30
    assert not service.status()['firmware_alive']
    service.last_state = time.monotonic()
    assert service.status()['firmware_alive']


def test_configure_switches_provider_and_keeps_credentials_separate(monkeypatch):
    monkeypatch.setenv('PHONE_VOICE_PROVIDER','doubao')
    monkeypatch.setenv('DOUBAO_API_KEY','synthetic-doubao')
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    async def scenario():
        service = PhoneService('fake')
        await service.command({'op':'configure','provider':'openai','api_key':'synthetic-openai'})
        assert service.provider=='openai'
        assert service.voice_key()=='synthetic-openai'
        await service.command({'op':'configure','provider':'doubao'})
        assert service.voice_key()=='synthetic-doubao'
        assert service.provider=='doubao'
    asyncio.run(scenario())


def test_mismatched_provider_cannot_dial(monkeypatch):
    monkeypatch.setenv('PHONE_VOICE_PROVIDER','doubao')
    async def scenario():
        service=PhoneService('fake')
        with pytest.raises(RuntimeError,match='渠道'):
            await service.command({'op':'call','provider':'openai','number':'13812345678','task':'测试'})
        assert service.current is None
    asyncio.run(scenario())


def test_api_verification_rejects_configuration_changed_while_connecting(monkeypatch):
    monkeypatch.setenv('PHONE_VOICE_PROVIDER', 'doubao')
    monkeypatch.setenv('DOUBAO_API_KEY', 'synthetic-only')
    async def scenario():
        service = PhoneService('fake')
        closed = []
        class Model:
            async def open(self):
                await service.command({'op':'configure','provider':'openai','api_key':'synthetic-openai'})
            async def close(self):
                closed.append(True)
        service.model = lambda *args: Model()
        with pytest.raises(RuntimeError, match='渠道设置已变化'):
            await service.command({'op':'check-api','provider':'doubao'})
        assert closed == [True]
    asyncio.run(scenario())


def test_configure_rejects_manual_phone_call(monkeypatch):
    monkeypatch.setenv('DOUBAO_API_KEY', 'synthetic-only')
    async def scenario():
        service = PhoneService('fake')
        service.state.call = 1
        with pytest.raises(RuntimeError, match='通话'):
            await service.command({'op':'configure','provider':'doubao'})
    asyncio.run(scenario())
