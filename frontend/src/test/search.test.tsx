import {afterEach,describe,expect,it,vi} from 'vitest';
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import SearchPanel from '../SearchPanel';
import {api} from '../bridge';
vi.mock('../bridge',()=>({api:vi.fn(),idempotencyKey:()=>crypto.randomUUID()}));
afterEach(()=>{cleanup();vi.clearAllMocks()});
const props={active:true,scope:'account1:session1',version:3,enabled:true,busy:false,act:vi.fn()};
const batch=(id:string,name:string)=>({id,songs:[{id,name,artist:'目录歌手',album:{id:'a1',name:'真实专辑'},match_basis:['作品目录']}],offset:0,total:1,expires_at:Date.now()/1000+300,query:{original:name}});
describe('isolated catalogue search',()=>{
 it('ignores an old response and preserves search when parent polls or tab changes',async()=>{
  let release:(value:unknown)=>void=()=>{};let count=0;
  vi.mocked(api).mockImplementation(async(endpoint,_method,params)=>{
   if(endpoint==='v2/action')return {task_id:'job'+(++count)};
   if((params as {id:string}).id==='job1')return await new Promise(r=>{release=r});
   return {state:'confirmed',result:{batch:batch('new','鸣潮')}};
  });
  const {rerender}=render(<SearchPanel {...props}/>);
  fireEvent.change(screen.getByRole('textbox',{name:'搜索歌曲'}),{target:{value:'旧查询'}});fireEvent.click(screen.getByRole('button',{name:'搜索'}));
  await waitFor(()=>expect(api).toHaveBeenCalledWith('v2/jobs','GET',{id:'job1'}));
  fireEvent.change(screen.getByRole('textbox',{name:'搜索歌曲'}),{target:{value:'鸣潮'}});fireEvent.click(screen.getByRole('button',{name:'搜索'}));
  await screen.findByText('真实专辑');
  await act(async()=>release({state:'confirmed',result:{batch:batch('old','无关旧结果')}}));
  expect(screen.queryByText('无关旧结果')).not.toBeInTheDocument();
  rerender(<SearchPanel {...props} version={4} active={false}/>);rerender(<SearchPanel {...props} version={5}/>);
  expect(screen.getByText('真实专辑')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'排入下一首'}));expect(props.act).toHaveBeenCalledWith('select',{batch_id:'new',song_id:'new'});
 });
 it('clears results after the trusted account/session changes',async()=>{
  vi.mocked(api).mockImplementation(async endpoint=>endpoint==='v2/action'?{task_id:'job'}:{state:'confirmed',result:{batch:batch('a','作品曲')}});
  const {rerender}=render(<SearchPanel {...props}/>);fireEvent.change(screen.getByRole('textbox',{name:'搜索歌曲'}),{target:{value:'作品曲'}});fireEvent.click(screen.getByRole('button',{name:'搜索'}));await screen.findByText('真实专辑');
  rerender(<SearchPanel {...props} scope="other-account:other-session"/>);expect(screen.queryByText('真实专辑')).not.toBeInTheDocument();
 });
});
