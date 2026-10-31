import {useEffect,useRef,useState} from 'react';
import {api,idempotencyKey} from './bridge';
import type {Song} from './types';
import {Icon} from './icons';
type SearchSong=Song & {match_basis?:string[];catalog_relation?:string;reason?:string};
type Batch={id:string;songs:SearchSong[];offset:number;total:number;next_offset?:number|null;expires_at:number;query?:{original:string;canonical:string;kind:string;model_query?:string;partial?:boolean;sources?:{route:string;state:string}[]}};
type Props={active:boolean;scope:string;version:number;enabled:boolean;busy:boolean;act:(action:string,payload?:Record<string,unknown>)=>Promise<unknown>};
export default function SearchPanel({active,scope,version,enabled,busy,act}:Props){
 const [query,setQuery]=useState(''),[kind,setKind]=useState('auto'),[batch,setBatch]=useState<Batch>(),[loading,setLoading]=useState(false),[error,setError]=useState('');
 const sequence=useRef(0),mounted=useRef(true);
 useEffect(()=>{setBatch(undefined);setLoading(false);sequence.current++},[scope]);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;sequence.current++}},[]);
 const search=async(offset=0,paginate=false)=>{const generation=++sequence.current;setLoading(true);setError('');
  try{const {task_id}=await api<{task_id:string}>('v2/action','POST',{action:'search',payload:{query,kind,...(paginate?{batch_id:batch?.id,offset}:{})},expected_version:version,idempotency_key:idempotencyKey()});const started=Date.now();
   while(mounted.current&&generation===sequence.current&&Date.now()-started<100000){const job=await api<{state:string;result:{batch?:Batch;reason?:string;state:string}}>('v2/jobs','GET',{id:task_id});if(!['pending','running'].includes(job.state)){if(!job.result?.batch)throw new Error(job.result?.reason||'搜索失败，请重试');if(generation===sequence.current)setBatch(job.result.batch);return}await new Promise(r=>setTimeout(r,500))}if(generation===sequence.current)throw new Error('搜索超时，请重试');
  }catch(e){if(mounted.current&&generation===sequence.current)setError((e as Error).message)}finally{if(mounted.current&&generation===sequence.current)setLoading(false)}};
 return <section className="search-workspace" style={{display:active?'block':'none'}} aria-label="音乐目录搜索">
  <div className="page-heading"><div><h1>下一首，找对味。</h1><p>找一首歌，也可以找一整个故事的原声。</p></div></div>
  <form className="catalog-search" onSubmit={e=>{e.preventDefault();void search()}}><Icon name="search"/><input aria-label="搜索歌曲" value={query} maxLength={160} placeholder="歌名、歌手、作品原声、歌词…" onChange={e=>setQuery(e.target.value)}/><select aria-label="搜索类型" value={kind} onChange={e=>setKind(e.target.value)}><option value="auto">自动识别</option><option value="work">作品原声</option><option value="song">歌曲</option><option value="artist">歌手</option><option value="lyrics">歌词片段</option><option value="mood">氛围</option></select><button className="primary" disabled={!query.trim()}>搜索</button></form>
  <div className="search-status" aria-live="polite">{loading?'正在核对平台歌曲与作品目录…':batch?`${batch.query?.original||query} · ${batch.total} 条真实结果 · 每页 20 首`:'支持不完整歌名与作品别名，结果以实际音乐目录为准。'}{batch?.query?.model_query&&<span>补充查询：{batch.query.model_query}</span>}</div>
  {error&&<p role="alert" className="notice error">{error} <button className="text-btn" onClick={()=>void search()}>重新搜索</button></p>}
  {batch?.query?.partial&&<p className="search-partial">部分目录来源暂不可用，已保留找到的结果。可稍后重新搜索。</p>}
  {!!batch&&<div className="catalog-list" aria-busy={loading}>{batch.songs.map((song,index)=><article className="catalog-row" key={song.id}><span className="catalog-number">{batch.offset+index+1}</span><div className="catalog-cover">{(song.cover_url||song.coverUrl)?<img src={song.cover_url||song.coverUrl} alt="" loading="lazy" onError={e=>{e.currentTarget.style.visibility='hidden'}}/>:<Icon name="music"/>}</div><div className="catalog-title"><strong>{song.name}</strong><span>{song.artist||'未知歌手'}</span><small>{song.catalog_relation||'目录匹配'} · {(song.match_basis||[]).join('、')||song.reason||'实际平台结果'}</small></div><span className="catalog-album">{song.album?.name||'专辑信息未知'}</span><div className="catalog-actions"><button className="outline" disabled={!enabled||busy} onClick={()=>void act('select',{batch_id:batch.id,song_id:song.id})}>排入下一首</button><button className="text-btn" disabled={!enabled||busy} onClick={()=>void act('play_now',{batch_id:batch.id,song_id:song.id})}>现在听</button></div></article>)}{!batch.songs.length&&<div className="empty"><h3>这次还没有可靠匹配。</h3><p>补充歌手、作品名或换一种搜索类型试试。</p></div>}</div>}
  {!!batch&&<div className="search-pagination"><button className="outline" disabled={loading||!batch.offset} onClick={()=>void search(Math.max(0,batch.offset-20),true)}>上一页</button><span>{batch.total?batch.offset+1:0}–{batch.offset+batch.songs.length} / {batch.total}</span><button className="outline" disabled={loading||batch.next_offset==null} onClick={()=>void search(batch.next_offset||0,true)}>下一页</button></div>}
 </section>
}
