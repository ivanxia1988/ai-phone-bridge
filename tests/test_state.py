from phone_bridge.state import DeviceState


def test_dial_queued_is_not_connection_and_setup_idle_is_not_active_end():
    state = DeviceState()
    state.update("resp:dial queued")
    assert not state.active
    state.update("evt:setup=2")
    assert state.setup == 2
    state.update("evt:call_active")
    state.update("evt:setup=0")
    assert state.active
    state.update("evt:call_idle")
    assert not state.active


def test_rate_from_negotiated_audio_and_disconnect_clears_call():
    state = DeviceState()
    state.update("state:conn=3 audio=3 call=1 setup=0 held=0 rate=16000 firmware=1 summary=in_call")
    assert state.connected and state.audio and state.active and state.rate == 16000
    state.update("evt:disconnected")
    assert not state.connected and not state.audio and not state.active
