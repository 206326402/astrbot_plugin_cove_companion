import assert from 'node:assert/strict';
import test from 'node:test';
import {parseSong} from '../src/netease/client.js';

test('catalogue parsing preserves album, artist identities, aliases and translated titles',()=>{
 const song=parseSong({id:123,name:'Test',dt:180000,ar:[{id:321,name:'Artist'}],al:{id:456,name:'鸣潮 原声集',picUrl:'https://example.invalid/a.jpg'},alia:['别名'],tns:['译名']});
 assert.ok(song);assert.equal(song.id,'123');assert.equal(song.album?.id,'456');assert.equal(song.album?.name,'鸣潮 原声集');
 assert.equal(song.artists?.[0]?.id,'321');assert.deepEqual(song.aliases,['别名']);assert.deepEqual(song.translations,['译名']);
});
test('legacy catalogue shapes preserve available metadata and reject missing identities',()=>{
 assert.equal(parseSong(null),null);assert.equal(parseSong({name:'missing'}),null);
 const song=parseSong({id:'77',name:'Song',duration:42000,artists:[{id:9,name:'A'}],album:{id:8,name:'Album'},alias:['alias'],transNames:['translated']});
 assert.ok(song);assert.equal(song.artists?.[0]?.name,'A');assert.equal(song.album?.name,'Album');assert.deepEqual(song.aliases,['alias']);
});
