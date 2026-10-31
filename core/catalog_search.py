"""Bounded catalogue retrieval. Model rewrites are queries, never song evidence."""
from __future__ import annotations
import asyncio
import difflib
import json
import re
import time
import unicodedata
from pathlib import Path

try:
    from opencc import OpenCC
    _converter = OpenCC('t2s')
except ImportError:
    _converter = None

ALIASES = json.loads(Path(__file__).with_name('catalog_aliases.json').read_text(encoding='utf-8'))


def normalize(value):
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    if _converter: text = _converter.convert(text)
    return re.sub(r'[^\w\u3400-\u9fff]+', ' ', text).strip()


def describe_query(query, kind='auto'):
    query = str(query or '').strip()[:160]
    if not query: raise ValueError('query_required')
    if kind not in {'auto','song','artist','work','lyrics','mood'}: raise ValueError('search_kind_invalid')
    norm = normalize(query)
    entity = next((v for v in ALIASES if any(normalize(a) in norm for a in v['aliases'])), None)
    if kind == 'auto':
        kind = 'work' if entity or re.search(r'(原声|原聲|配乐|配樂|ost|bgm)',norm) else 'lyrics' if re.search(r'(歌词|歌詞|有一句|唱的是)',norm) else 'mood' if re.search(r'(来点|來點|氛围|氛圍|适合|適合|风格|風格)',norm) else 'song'
    aliases = entity['aliases'] if entity else [query]
    canonical = entity['name'] if entity else re.sub(r'(的?原声|的?原聲|ost|bgm|配乐|配樂)', '', query, flags=re.I).strip() or query
    return {'original':query,'kind':kind,'canonical':canonical,'aliases':list(dict.fromkeys([canonical]+aliases)),
            'alias_source':entity.get('source') if entity else 'user_query'}


def catalogue_text(song):
    album = song.get('album') or {}
    return normalize(' '.join([str(song.get('name') or ''), str(song.get('artist') or ''),
                                str(album.get('name') if isinstance(album,dict) else album),
                                ' '.join(song.get('aliases') or []), ' '.join(song.get('translations') or [])]))


class CatalogueSearch:
    async def search_catalog(self, query, kind='auto', *, allow_model=True, dry_run=False):
        started=time.monotonic();spec = describe_query(query, kind); found=[]; sources=[]; calls=0
        sem=asyncio.Semaphore(3)
        async def read(route, word='', ident=''):
            nonlocal calls
            if calls >= 8: return {}
            calls += 1
            async with sem:
                try:
                    if hasattr(self.client,'catalog'):
                        result=await asyncio.wait_for(self.client.catalog(route,word,ident,30),6)
                    elif route=='songs': result={'songs':await asyncio.wait_for(self.client.search(word,30),6)}
                    else: return {}
                    sources.append({'route':route,'query':word,'id':ident,'state':'ready'})
                    return result
                except Exception as exc:
                    sources.append({'route':route,'query':word,'state':'unavailable','reason':getattr(exc,'code',type(exc).__name__)})
                    return {}
        def collect(result, route, word, association=None):
            for s in result.get('songs') or []:
                if not str(s.get('id') or '').isdigit(): continue
                found.append({**s,'id':str(s['id']),'candidate_source':route,'query':word,
                              **({'work_evidence':association} if association else {})})
        async def retrieve():
            words=spec['aliases'][:2] if spec['kind']=='work' else [spec['original']]
            routes=[('lyrics' if spec['kind']=='lyrics' else 'songs',w) for w in words]
            if spec['kind']=='work': routes += [('albums',spec['canonical']),('artists',spec['canonical'])]
            elif spec['kind']=='artist': routes += [('artists',spec['canonical'])]
            results=await asyncio.gather(*(read(r,w) for r,w in routes))
            expand=[]
            for (route,word),result in zip(routes,results):
                collect(result,route,word)
                for item in (result.get('items') or [])[:3]:
                    if not str(item.get('id') or '').isdigit():continue
                    if not any(normalize(a) in normalize(item.get('name')) for a in spec['aliases']):continue
                    if route in {'albums','artists'}:
                        expand.append((route,item))
            for route,item in expand[:3]:
                detail=await read('album_tracks' if route=='albums' else 'artist_tracks',ident=str(item['id']))
                collect(detail,route,spec['canonical'],{'kind':route,'id':str(item['id']),'name':item.get('name'),'source':'netease_catalog'})
        try: await asyncio.wait_for(retrieve(),12)
        except asyncio.TimeoutError: sources.append({'state':'partial','reason':'catalog_deadline'})
        def ranked():
            seen={}
            for song in found[:240]:
                text=catalogue_text(song); aliases=[normalize(a) for a in spec['aliases']]
                matched=[a for a in aliases if a and a in text]
                score=max([difflib.SequenceMatcher(None,normalize(spec['original']),normalize(song.get('name'))).ratio()]+[.88 if a in normalize(song.get('name')) else .8 for a in matched])
                rewritten=song.get('candidate_source') in {'suggestion','model_query_verified_by_catalog'} and normalize(song.get('query')) in text
                if rewritten:score=max(score,.79)
                associated=bool(matched or song.get('work_evidence'))
                if spec['kind']=='work' and not associated:continue
                if spec['kind'] not in {'mood','lyrics','work'} and score<.42:continue
                if song.get('work_evidence'):score=max(score,.9)
                label='翻唱／改编' if re.search(r'(翻唱|翻自|cover|钢琴版|鋼琴版|改编)',text) else '同人／二创' if re.search(r'(同人|二创|二創)',text) else '作品目录关联' if spec['kind']=='work' and associated else '目录匹配'
                song={**song,'search_score':round(score,3),'match_basis':matched or ([song['work_evidence']['name']] if song.get('work_evidence') else ['补充查询：'+song['query']] if rewritten else ['平台歌词检索' if spec['kind']=='lyrics' else '模糊目录匹配']),
                      'catalog_relation':label,'identity_verified':False,'reason':label+'：'+('、'.join(matched) if matched else '依据实际平台检索及目录关系')}
                old=seen.get(song['id'])
                if not old or score>old['search_score']: seen[song['id']]=song
            return sorted(seen.values(),key=lambda s:-s['search_score'])[:120]
        songs=ranked()
        if len(songs)<3 and calls<8:
            suggestion=await read('suggest',spec['original'])
            values=suggestion.get('suggestions') or []
            # A close real suggestion is useful for misspellings without an LLM.
            related=[v for v in values if difflib.SequenceMatcher(None,normalize(v),normalize(query)).ratio()>=.45]
            if related and calls<8:
                collect(await read('songs',related[0]),'suggestion',related[0]);songs=ranked()
        if len(songs)<3 and allow_model and not dry_run and hasattr(self.agent,'rewrite_music_query') and calls<8:
            budget=self.store.token_summary(self.settings.token_daily_limit)
            if not budget['reached'] and budget['meter_available']:
                rewritten=await self.agent.rewrite_music_query(query=query,kind=spec['kind'])
                if rewritten and normalize(rewritten)!=normalize(query):
                    spec['model_query']=rewritten
                    collect(await read('songs',rewritten),'model_query_verified_by_catalog',rewritten);songs=ranked()
        if not songs:
            # Bounded local history is another catalogue-backed typo route.
            session=self.store.active_session() or {'owner_umo':self.owner_umo}
            for h in self.store.scoped_history(session,100):
                if difflib.SequenceMatcher(None,normalize(query),normalize(h.get('song_name'))).ratio()>.55:
                    found.append({'id':str(h['song_id']),'name':h.get('song_name'),'artist':h.get('artist'),'candidate_source':'local_playback'})
            songs=ranked()
        return {'songs':songs,'query':spec,'sources':sources,'upstream_requests':calls,'partial':any(s.get('state')!='ready' for s in sources),'model_called':bool(spec.get('model_query')),'elapsed_ms':round((time.monotonic()-started)*1000,2)}

    async def search_result(self, session, query, kind='auto', batch_id=None, offset=0):
        offset=max(0,int(offset))
        if batch_id:
            batch=self.store.search_batch(session,batch_id)
            if not batch:raise ValueError('candidate_batch_expired')
        else:
            spec=describe_query(query,kind)
            with self.store.lock:
                rows=self.store.conn.execute('SELECT id,query_json FROM music_search_batches WHERE scope_key=? AND session_id IS ? AND expires_at>? ORDER BY created_at DESC LIMIT 20',
                                             (self.store.scope_key(session),session.get('id'),time.time())).fetchall()
            cached=next((r for r in rows if json.loads(r['query_json']).get('original')==spec['original'] and json.loads(r['query_json']).get('kind')==spec['kind']),None)
            batch=self.store.search_batch(session,cached['id']) if cached else None
            if not batch:
                result=await self.search_catalog(query,kind)
                batch=self.store.save_search_batch(session,result['songs'],{**result['query'],'sources':result['sources'],'partial':result['partial']})
                self.store.decision(session.get('id'),'search',query=result['query'],count=len(result['songs']),sources=result['sources'],elapsed_ms=result.get('elapsed_ms'))
        total=len(batch['songs'])
        return {**batch,'songs':batch['songs'][offset:offset+20],'offset':offset,'total':total,'next_offset':offset+20 if offset+20<total else None}
