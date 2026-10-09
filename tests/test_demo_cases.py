import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def test_headhunter_case_click_fills_only_prompt_without_dialing():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for frontend interaction test')
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const fields = new Map();
const field = id => {
  if (!fields.has(id)) fields.set(id, {value:'',classList:{toggle(){}},focus(){this.focused=true;},setSelectionRange(start,end){this.selection=[start,end];}});
  return fields.get(id);
};
field('task').value='原任务';
field('number').value='00000000001';
field('voice').value='zh_female_vv_jupiter_bigtts';
field('limit').value='120';
const requests=[];
const context={document:{getElementById:field},setInterval(){},fetch(path,options){
  requests.push({path,method:options?.method||'GET'});
  return new Promise(()=>{});
}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('phone_bridge/web/app.js','utf8'),context);
assert.equal(typeof field('headhunterDemo').onclick,'function','demo button must handle click');
field('headhunterDemo').onclick();
const prompt=field('task').value;
for (const part of ['人设','风格','任务','AI','初聊','结束','语调','肯定','顾虑']) assert.ok(prompt.includes(part),part);
assert.ok(!/(模拟|扮演|演示|虚构)/.test(prompt),'case must use direct recruiting conversation');
assert.ok(prompt.length>300 && prompt.length<3500,'complete prompt must fit call limits');
assert.equal(field('number').value,'00000000001');
assert.equal(field('voice').value,'zh_female_vv_jupiter_bigtts');
assert.equal(field('limit').value,'120');
assert.ok(field('task').focused);
assert.deepEqual(field('task').selection,[0,0]);
assert.equal(field('task').scrollTop,0);
assert.equal(requests.filter(r=>r.method==='POST').length,0,'case must not call backend');
const html=fs.readFileSync('phone_bridge/web/index.html','utf8');
assert.match(html,/<option value="" selected>手动输入号码/);
assert.match(html,/id="number"[^>]*value=""/);
assert.ok(!html.includes("00000000001") && !html.includes("00000000002"));
assert.equal(typeof field('savedNumber').onchange,'function');
field('savedNumber').value='00000000002';
field('savedNumber').onchange();
assert.equal(field('number').value,'00000000002');
assert.equal(field('task').value,prompt);
assert.equal(field('voice').value,'zh_female_vv_jupiter_bigtts');
assert.equal(field('limit').value,'120');
field('number').value='13812345678';
field('number').oninput();
assert.equal(field('savedNumber').value,'');
field('savedNumber').onchange();
assert.equal(field('number').value,'13812345678','custom option must preserve entered number');
assert.equal(requests.filter(r=>r.method==='POST').length,0);
assert.match(html,/type="button" id="headhunterDemo"/,'case must not submit form');
'''
    result = subprocess.run([node, '-'], input=script, text=True, cwd=ROOT, capture_output=True)
    assert result.returncode == 0, result.stderr
