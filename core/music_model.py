"""Small, metered music requests sharing the room agent's model lock."""
from __future__ import annotations
import asyncio
import json
import re
import time
from .text import clean
from .metering import estimate


def introduction(text, song, reason='', user=False):
    text=clean(text,80)
    name=clean(song.get('name') or song.get('id'),32)
    # Audit reasons belong in the workbench. Model output and short-text padding
    # must never leak scoring/debug fields into a room message.
    if re.search(r'候选|排序|权重|注释|算法|匹配分|数据库|rank_score|explanation|keyword_search|token_request',text,re.I):text=''
    if not text:
        text=('这首《'+name+'》是你选的，想陪你一起听。' if user else '找到《'+name+'》，想和你一起听。')
    if name not in text:
        text=('你选的' if user else '想陪你听')+'《'+name+'》。'+text
    if len(text)<35:
        text+=('听完再聊聊你的感受，也可以随时换一首，我陪着你。' if user else '这次先一起试听，听完告诉我合不合口味，也可以随时换一首。')
    if len(text)<35:
        text+='先一起听听，你可以随时告诉我是否合你的口味，我会记住你的明确反馈。'
    return text[:80]


class MusicModel:
    async def music_json(self, task, data, session=None, kind='autodj'):
        async with self._lock:
            provider=await self.provider_id()
            conversation=await self._conversation()
            _,persona,_=await self._persona(conversation)
            prompt=json.dumps({'task':task,'data':data},ensure_ascii=False)
            for attempt in range(2):
                call_id=self._reserve(kind)
                if not call_id:raise RuntimeError('model_budget_exhausted')
                token_key='music:'+call_id; started=time.monotonic()
                try:
                    self.store.reserve_tokens(token_key,self.store.scope_key(session),kind,estimate({'system':persona,'prompt':prompt}),1200,self.settings.token_daily_limit,True)
                    try:
                        response=await asyncio.wait_for(self.context.llm_generate(chat_provider_id=provider,prompt=prompt,system_prompt=persona,contexts=[]),timeout=self.settings.model_timeout_seconds)
                        self.store.settle_tokens(token_key,response=response)
                    except BaseException:
                        self.store.settle_tokens(token_key,failed=True);raise
                    text=str(getattr(response,'completion_text',''))
                    match=re.search(r'\{[\s\S]*\}',text)
                    result=json.loads(match.group(0) if match else text)
                    if not isinstance(result,dict):raise ValueError('invalid_music_json')
                    self.store.finish_model_call(call_id,'success',int((time.monotonic()-started)*1000))
                    return {**result,'token_request_id':token_key}
                except Exception as exc:
                    self.store.finish_model_call(call_id,'failed',int((time.monotonic()-started)*1000),type(exc).__name__)
                    status=getattr(exc,'status_code',0) or getattr(exc,'status',0)
                    transient=isinstance(exc,(asyncio.TimeoutError,ConnectionError)) or status==429 or (isinstance(status,int) and status>=500)
                    if attempt==0 and transient:
                        await asyncio.sleep(1);continue
                    raise

    async def rewrite_music_query(self,query,kind):
        try:
            result=await self.music_json('将模糊音乐搜索改写为一个可检索的歌名、作品或艺人关键词。只返回JSON {query:string}；保留原意，不声称找到了歌曲。',{'query':query,'kind':kind})
            return clean(result.get('query'),160)
        except Exception:return ''

    async def introduce_user_song(self,session,song):
        verified=bool(song.get('selection_verified'))
        attribution='用户亲自点了这首歌，承认这是用户的选择。' if verified else '这首正在播放，来源尚未核实，不能声称是用户选的或自己推荐的。'
        result=await self.music_json(attribution+'按当前人格自然回应一句35至80字。仅根据目录事实表达陪伴或好奇，不编造听感、歌词、制作信息。不要向用户复述“来源尚未核实、目录、候选”等内部状态。称呼沿用人格。返回JSON {text:string}。',
                                     {k:song.get(k) for k in ('id','name','artist','album','aliases')},session,'proactive')
        return introduction(result.get('text'),song,'这首已经在房间里播放，先一起听听，再聊聊你的感受。' if not verified else '',user=verified)

    async def rank_candidates(self, *, session,candidates,state):
        compact=[{k:s.get(k) for k in ('id','name','artist','album','aliases','tags','rank_score','explanation','candidate_source','exploration')} for s in candidates[:12]]
        try:
            result=await self.music_json('从已排序候选选2至3首；用户本场需求和明确排除必须遵守。JSON {songs:[{id,reason,intro}]}；id必须来自候选，最多一首exploration。reason引用候选中的实际依据；intro按当前人格写35至80字，包含歌名及为何现在推荐，称呼沿用人格。介绍像给熟悉的人递来一首歌：把真实偏好翻译成自然表达，例如“你之前提过想听这个方向，找到《歌名》想和你试试，听完告诉我合不合口味”。介绍不得复述系统术语：候选、排序、权重、注释、算法、匹配分、数据库；这些依据只放reason。依据少时自然承认只是想一起试听。不得编造歌词、听感、曲风或音频属性。',{'state':state,'candidates':compact},session)
            if not isinstance(result.get('songs'),list):raise ValueError('invalid_ranking')
            return result
        except Exception as exc:
            reason=str(exc) if str(exc) in {'token_budget_reached','token_meter_unavailable'} else getattr(exc,'code',type(exc).__name__)
            return {'state':'budget_exhausted' if reason.startswith('token_') else 'failed','songs':[],'reason':reason}
