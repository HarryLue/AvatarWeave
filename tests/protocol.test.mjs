import test from 'node:test';
import assert from 'node:assert/strict';
import { parseAvatarCommand } from '../packages/core/dist/protocol.js';
test('protocol accepts v1 commands and rejects unknown or malformed payloads', () => {
  assert.equal(parseAvatarCommand({version: 1, id: 'test', type: 'motion', name: '', index: 3}).index, 3);
  for (const value of [null, {}, {version: 2, id: 'x', type: 'motion', name: ''}, {version: 1, id: 'x', type: 'script', name: 'evil'}, {version: 1, id: 'x', type: 'motion', name: '', index: -1}]) assert.equal(parseAvatarCommand(value), undefined);
});

test('current protocol rejects unknown fields, unsafe indices and invalid expression payloads', () => {
  for (const value of [
    {version:1,id:'',type:'motion',name:''},
    {version:true,id:'x',type:'motion',name:''},
    {version:1,id:'x',type:'motion',name:'',index:true},
    {version:1,id:'x',type:'motion',name:'',index:9007199254740992},
    {version:1,id:'x',type:'expression',name:''},
    {version:1,id:'x',type:'expression',name:'Smile',index:1},
    {version:1,id:'x',type:'motion',name:'',script:'x'},
  ]) assert.equal(parseAvatarCommand(value),undefined);
});
