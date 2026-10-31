"""Request-level accounting. Never patch a shared provider instance."""
from __future__ import annotations
import json
import math
import uuid


def field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def usage_of(response):
    usage = field(response, 'usage')
    if usage is None:
        raw = field(response, 'raw_completion')
        usage = field(raw, 'usage') or field(raw, 'usage_metadata')
    if usage is None:
        return None
    incoming = field(usage, 'input')
    if incoming is None:
        incoming = field(usage, 'prompt_tokens', field(usage, 'input_tokens',field(usage,'prompt_token_count')))
    if incoming is None and field(usage, 'input_other') is not None:
        incoming = field(usage, 'input_other', 0) + field(usage, 'input_cached', 0)
    outgoing = field(usage, 'output', field(usage, 'completion_tokens', field(usage, 'output_tokens',field(usage,'candidates_token_count'))))
    try:
        if incoming is None or outgoing is None:
            return None
        incoming, outgoing = int(incoming), int(outgoing)
        return (incoming, outgoing) if incoming >= 0 and outgoing >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def estimate(value):
    """Conservative UTF-8 byte estimate, explicitly not provider usage."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')
        return max(1, math.ceil(len(encoded) / 2))
    except Exception:
        return None


class MeteredProvider:
    def __init__(self, provider, store, scope, kind, limit, automatic):
        self.provider, self.store, self.scope = provider, store, scope
        self.kind, self.limit, self.automatic = kind, limit, automatic

    def __getattr__(self, key):
        return getattr(self.provider, key)

    async def text_chat(self, **payload):
        key = 'request:' + uuid.uuid4().hex
        estimated_input = estimate(payload)
        # Reserve input plus a bounded output allowance before network I/O.
        self.store.reserve_tokens(key, self.scope, self.kind, estimated_input, 4096,
                                  self.limit, self.automatic)
        try:
            response = await self.provider.text_chat(**payload)
            self.store.settle_tokens(key, response=response, fallback_input=estimated_input)
            return response
        except BaseException:
            self.store.settle_tokens(key, failed=True)
            raise


class SharedTurnMeter:
    """Buffer the whole QQ turn; charge it only if a music tool is used.

    This proxy belongs to one runner, never the global provider. Streaming
    chunks are not separate requests; the final usage settles their one row.
    """
    def __init__(self,provider,store,event,scope,limit,ledger=None):
        self.provider,self.store,self.event,self.scope,self.limit=provider,store,event,scope,limit
        self.ledger=ledger if ledger is not None else []
    def __getattr__(self,key):return getattr(self.provider,key)
    def flush(self):
        if not self.event.get_extra('cove_shared_music_used',False):return
        for item in self.ledger:
            if item.get('recorded'):continue
            self.store.reserve_tokens(item['key'],self.scope,'qq_shared',item['incoming'],4096,self.limit,shared=True)
            self.store.settle_tokens(item['key'],response=item.get('response'),fallback_input=item['incoming'],failed=item.get('failed',False))
            item['recorded']=True
    def _finish(self,payload,response=None,failed=False):
        self.ledger.append({'key':'shared:'+uuid.uuid4().hex,'incoming':estimate(payload),'response':response,'failed':failed})
        self.flush()
    async def text_chat(self,**payload):
        try:response=await self.provider.text_chat(**payload)
        except BaseException:
            self._finish(payload,failed=True);raise
        self._finish(payload,response);return response
    async def text_chat_stream(self,**payload):
        finalized=False
        try:
            async for response in self.provider.text_chat_stream(**payload):
                # AstrBot breaks its iterator as soon as it sees the final item.
                # Settle before yielding it, rather than relying on generator tail.
                if not field(response,'is_chunk',False):
                    self._finish(payload,response);finalized=True
                yield response
        except BaseException:
            if not finalized:self._finish(payload,failed=True)
            raise
        if not finalized:self._finish(payload,failed=True)


async def tool_loop(agent, event, provider_id, prompt, contexts, tools, system, max_steps):
    """AstrBot runner with an instance-local proxy meters every tool continuation."""
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.entities import ProviderRequest
    provider = await agent.context.provider_manager.get_provider_by_id(provider_id)
    if provider is None:
        raise RuntimeError('provider_missing')
    runner = ToolLoopAgentRunner()
    await runner.reset(provider=provider,
        request=ProviderRequest(prompt=prompt, contexts=contexts, system_prompt=system, func_tool=tools),
        run_context=AgentContextWrapper(context=AstrAgentContext(context=agent.context,event=event),tool_call_timeout=15),
        tool_executor=FunctionToolExecutor(), agent_hooks=BaseAgentRunHooks(), streaming=False)
    kind = event.get_extra('cove_room_kind')
    runner.provider = MeteredProvider(provider, agent.store, agent.store.scope_key(), kind,
                                    agent.settings.token_daily_limit, kind != 'room_reply')
    async for _ in runner.step_until_done(max_steps):
        pass
    result = runner.get_final_llm_resp()
    if result is None:
        raise RuntimeError('empty_model_response')
    return result
