import json

import pytest

from phone_bridge.ui import ConsoleController


class MemoryCredentials:
    def __init__(self):
        self.data = {}
    def has(self, provider):
        return provider in self.data
    def get(self, provider):
        return self.data.get(provider, '')
    def set(self, provider, key):
        self.data[provider] = key


def controller(root, store, rpc=lambda _: {}):
    return ConsoleController(root=root, ports_fn=lambda: [], rpc_call=rpc, credential_store=store)


def test_credentials_survive_new_controller_without_files_or_status_leak(tmp_path):
    store = MemoryCredentials()
    first = controller(tmp_path, store)
    first.configure({'provider':'doubao','api_key':'synthetic-doubao-key'})
    second = controller(tmp_path, store)
    assert second.status()['credential_saved']
    assert second.api_key == 'synthetic-doubao-key'
    assert 'synthetic-doubao-key' not in json.dumps(second.status())
    assert 'synthetic-doubao-key' not in (tmp_path/'work'/'ui-preferences.json').read_text()


def test_providers_keep_separate_keys_and_switch_invalidates_verification(tmp_path):
    store = MemoryCredentials()
    ui = controller(tmp_path, store)
    ui.configure({'provider':'doubao','api_key':'synthetic-doubao-key'})
    ui.verified_voice = ui.voice_id
    ui.configure({'provider':'openai','api_key':'synthetic-openai-key'})
    assert ui.provider == 'openai'
    assert ui.api_key == 'synthetic-openai-key'
    assert not ui.status()['api_ready']
    ui.configure({'provider':'doubao','api_key':''})
    assert ui.api_key == 'synthetic-doubao-key'
    assert [p['id'] for p in ui.status()['providers'] if p['saved']] == ['doubao','openai']


def test_provider_change_rejected_during_call(tmp_path):
    ui = controller(tmp_path, MemoryCredentials(), rpc=lambda _: {'current_call':{'state':'talking'}})
    with pytest.raises(RuntimeError,match='通话'):
        ui.configure({'provider':'openai','api_key':'synthetic-only'})
    assert ui.provider == 'doubao'


def test_openai_call_never_sends_doubao_voice(tmp_path):
    store = MemoryCredentials()
    store.set('openai','synthetic-openai-key')
    calls = []
    backend = {'port':'/dev/cu.test','firmware':1,'phone_connected':True,
               'voice_provider':'openai','voice_credentials_available':True}
    def rpc(payload):
        calls.append(payload)
        return backend if payload['op']=='status' else {'id':'test-only'}
    ui = ConsoleController(root=tmp_path, ports_fn=lambda:['/dev/cu.test'], rpc_call=rpc,credential_store=store)
    ui.configure({'provider':'openai'})
    ui.verified_voice = ui.voice_id
    ui.call({'number':'13812345678','task':'测试','max_seconds':120})
    assert calls[-1]['provider']=='openai'
    assert 'voice_id' not in calls[-1]


def test_keychain_save_failure_does_not_claim_saved_or_change_provider(tmp_path):
    class Unavailable(MemoryCredentials):
        def set(self,provider,key):
            raise RuntimeError('钥匙串无法访问')
    ui = controller(tmp_path,Unavailable())
    with pytest.raises(RuntimeError,match='钥匙串'):
        ui.configure({'provider':'openai','api_key':'synthetic-only'})
    assert ui.provider=='doubao'
    assert not ui.status()['credential_saved']
