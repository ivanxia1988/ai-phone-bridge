import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from phone_bridge.live_transcript import LiveTranscript
from phone_bridge.service import PhoneCall, PhoneService
from phone_bridge.recording import CallRecording
from phone_bridge.audio import Playback


def test_drafts_are_replaced_and_final_text_cannot_regress():
    live = LiveTranscript()
    live.update('remote', 'q1', '我在上', 1)
    live.update('remote', 'q1', '我在上海', 2)
    live.update('agent_generated', 'r1', '明白，', 3, delta=True)
    live.update('agent_generated', 'r1', '您考虑什么机会？', 4, delta=True)
    live.interrupt('r1')
    live.update('remote', 'q1', '我在上海。', 5, final=True)
    live.update('remote', 'q1', '旧稿', 6)
    rows = live.snapshot()
    assert len(rows) == 2
    assert rows[0]['text'] == '我在上海。' and rows[0]['final']
    assert rows[0]['seconds'] == 1
    assert rows[1]['text'] == '明白，您考虑什么机会？'
    assert rows[1]['interrupted']
    rows[0]['text'] = 'external mutation'
    assert live.snapshot()[0]['text'] == '我在上海。'


def test_live_cache_is_bounded_and_marks_long_text():
    live = LiveTranscript()
    for n in range(100):
        live.update('remote', str(n), '测' * 3000, n)
    rows = live.snapshot()
    assert len(rows) == 60
    assert rows[0]['item_id'] == '40'
    assert len(rows[-1]['text']) == 2000 and rows[-1]['truncated']


def test_interrupt_before_text_is_remembered_with_a_bound():
    live = LiveTranscript()
    for n in range(100):
        live.interrupt(str(n))
    live.update('agent_generated', '99', '迟到的文字', 5, final=True)
    live.update('agent_generated', '0', '很早的文字', 6, final=True)
    assert live.snapshot()[0]['interrupted']
    assert not live.snapshot()[1]['interrupted']


def test_streaming_events_visible_before_final_but_only_finals_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr('phone_bridge.service.ROOT', tmp_path)
    async def scenario():
        service = PhoneService('fake')
        call = PhoneCall(service, '13812345678', '测试', 10)
        service.current = call
        call.recording = CallRecording(call.path, 8000)
        call.playback = Playback(8000)
        service.transport = type('Transport', (), {'control': lambda *_: None})()
        events = asyncio.Queue()
        class Model:
            async def receive(self):
                return await events.get()
        call.realtime = Model()
        worker = asyncio.create_task(call.receive_model(8000))
        try:
            events.put_nowait({'type':'conversation.item.input_audio_transcription.partial','item_id':'q1','transcript':'临时识别'})
            events.put_nowait({'type':'response.output_audio_transcript.delta','item_id':'r1','delta':'你好'})
            await asyncio.sleep(0)
            rows = service.status()['current_call']['live_transcripts']
            assert [r['text'] for r in rows] == ['临时识别','你好']
            assert not (call.path / 'transcripts.jsonl').read_text()
            events.put_nowait({'type':'conversation.item.input_audio_transcription.completed','item_id':'q1','transcript':'确认的识别文字'})
            events.put_nowait({'type':'response.output_audio_transcript.done','item_id':'r1','transcript':'你好。'})
            await asyncio.sleep(0)
            rows = service.status()['current_call']['live_transcripts']
            assert len(rows) == 2 and all(r['final'] for r in rows)
            entries = [json.loads(line) for line in (call.path / 'transcripts.jsonl').read_text().splitlines()]
            assert [e['text'] for e in entries] == ['确认的识别文字','你好。']
            assert 'live_transcripts' not in json.loads((call.path / 'result.json').read_text())
            assert call.error is None
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            call.recording.close(0)
    asyncio.run(scenario())


def test_console_reads_large_unicode_live_snapshot():
    from phone_bridge.ui import ConsoleController
    async def scenario(root):
        path = Path(root) / 'work' / 'phone.sock'
        path.parent.mkdir()
        snapshot = {'live_transcripts':[{'text':'测' * 2000} for _ in range(60)]}
        async def client(reader,writer):
            await reader.readline()
            writer.write((json.dumps({'ok':True,'result':snapshot},ensure_ascii=False)+'\n').encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        class Store:
            def get(self,_):
                return ''
        ui = ConsoleController(root=root,credential_store=Store())
        server = await asyncio.start_unix_server(client,path=str(path))
        async with server:
            result = await asyncio.to_thread(ui._request, {'op':'status'})
        assert len(result['live_transcripts']) == 60
        assert result == snapshot
    with tempfile.TemporaryDirectory(dir='/tmp') as root:
        asyncio.run(scenario(root))


def test_frontend_updates_rows_safely_and_keeps_scroll_position():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for frontend checks')
    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fields=new Map();
const element=()=>({textContent:'',children:[],scrollTop:0,scrollHeight:600,clientHeight:280,
  append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},classList:{toggle(){}}});
const field=id=>{if(!fields.has(id))fields.set(id,element());return fields.get(id)};
const context={document:{getElementById:field,createElement:element},fetch(){return new Promise(()=>{})},setTimeout(){}};
vm.createContext(context);vm.runInContext(fs.readFileSync('phone_bridge/web/app.js','utf8'),context);
const run=(text)=>vm.runInContext(text,context);
run(`renderLive({id:'call1',state:'talking',live_transcripts:[{role:'remote',item_id:'q1',text:'临时',seconds:1,final:false}]})`);
assert.equal(field('liveTranscript').children.length,1);
assert.equal(field('liveTranscript').children[0].children[1].textContent,'临时');
field('liveTranscript').scrollTop=15;field('liveTranscript').onscroll();
run(`renderLive({id:'call1',state:'talking',live_transcripts:[{role:'remote',item_id:'q1',text:'<img src=x onerror=alert(1)>',seconds:1,final:true},{role:'agent_generated',item_id:'r1',text:'请问…',seconds:2,final:false,interrupted:true}]})`);
assert.equal(field('liveTranscript').children.length,2,'final replaces draft');
assert.equal(field('liveTranscript').children[0].children[1].textContent,'<img src=x onerror=alert(1)>','use text content, not HTML');
assert.equal(field('liveTranscript').scrollTop,15,'reading position remains');
assert.match(field('liveTranscript').children[1].children[2].textContent,/已打断/);
run(`completedLive({result:{id:'call1',state:'completed'},transcripts:[{role:'remote',item_id:'q1',text:'最终识别',seconds:3},{role:'interrupt',item_id:'r1',text:'',seconds:4},{role:'agent_generated',item_id:'r1',text:'最后的AI文字',seconds:5}]})`);
assert.equal(field('liveTranscript').children.length,2);
assert.equal(field('liveTranscript').children[0].children[1].textContent,'最终识别');
assert.match(field('liveTranscript').children[1].children[2].textContent,/已打断/);
assert.match(field('liveStatus').textContent,/通话已结束/);
field('followLive').onclick();assert.equal(field('liveTranscript').scrollTop,600);
run(`renderLive({id:'call2',state:'talking',live_transcripts:[]})`);
assert.equal(field('liveTranscript').children.length,1);
assert.match(field('liveTranscript').children[0].textContent,/等待/,'new call clears old text');
'''
    result = subprocess.run([node,'-'],input=script,text=True,cwd=Path(__file__).resolve().parent.parent,capture_output=True)
    assert result.returncode == 0, result.stderr
