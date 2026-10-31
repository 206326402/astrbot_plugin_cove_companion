const FALLBACK_POLL_MS = 60_000;

export function buildListenerHtml(): string {
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Cove Bridge</title>
<style>
  :root{color-scheme:light dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
  *{box-sizing:border-box}
  body{margin:0;padding:12px;background:transparent;color:CanvasText}
  .card{padding:14px;border:1px solid color-mix(in srgb,CanvasText 16%,transparent);border-radius:14px;background:color-mix(in srgb,Canvas 92%,transparent)}
  .title{font-weight:650}.status{margin-top:9px;font-size:12px;opacity:.72}
  button{margin-top:12px;padding:8px 12px;border-radius:10px;border:1px solid color-mix(in srgb,CanvasText 18%,transparent);background:Canvas;color:CanvasText;font:inherit;cursor:pointer}
  button:disabled{opacity:.55;cursor:default}
</style>
</head>
<body>
<main class="card">
  <div class="title">Cove Bridge</div>
  <div id="status" class="status">已挂载，尚未监听。</div>
  <button id="toggle" type="button" disabled>正在连接…</button>
</main>
<script>
(() => {
  const FALLBACK_POLL_MS = ${FALLBACK_POLL_MS};
  const pending = new Map();
  let rpcId = 0;
  let timer = 0;
  let listening = false;
  let inFlight = false;
  let bridgeReady = false;
  let streamAbort = null;
  let streamGeneration = 0;
  let sseConnected = false;
  const DISPATCHED_STORAGE_KEY = 'cove-bridge-dispatched-v1';
  const PENDING_ACK_STORAGE_KEY = 'cove-bridge-pending-acks-v1';
  const PENDING_DISMISS_STORAGE_KEY = 'cove-bridge-pending-dismissals-v1';
  const MAX_RECENT_DISPATCHED = 128;
  const recentlyDispatched = new Set();
  const pendingAcks = new Set();
  const pendingDismissals = new Set();

  const statusEl = document.getElementById('status');
  const toggleEl = document.getElementById('toggle');
  const setStatus = (text) => { statusEl.textContent = text; };

  function readStoredIds(key) {
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || '[]');
      return Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : [];
    } catch {
      return [];
    }
  }

  function writeStoredIds(key, ids) {
    try {
      localStorage.setItem(key, JSON.stringify(ids));
    } catch {}
  }

  for (const id of readStoredIds(DISPATCHED_STORAGE_KEY)) recentlyDispatched.add(id);
  for (const id of readStoredIds(PENDING_ACK_STORAGE_KEY)) pendingAcks.add(id);
  for (const id of readStoredIds(PENDING_DISMISS_STORAGE_KEY)) pendingDismissals.add(id);

  function rememberDispatched(eventId) {
    recentlyDispatched.delete(eventId);
    recentlyDispatched.add(eventId);
    while (recentlyDispatched.size > MAX_RECENT_DISPATCHED) {
      const oldest = recentlyDispatched.values().next().value;
      if (!oldest) break;
      recentlyDispatched.delete(oldest);
    }
    writeStoredIds(DISPATCHED_STORAGE_KEY, [...recentlyDispatched]);
  }

  function rememberPendingAck(eventId) {
    pendingAcks.add(eventId);
    writeStoredIds(PENDING_ACK_STORAGE_KEY, [...pendingAcks]);
  }

  function forgetPendingAck(eventId) {
    pendingAcks.delete(eventId);
    writeStoredIds(PENDING_ACK_STORAGE_KEY, [...pendingAcks]);
  }

  function rememberPendingDismissal(eventId) {
    pendingDismissals.add(eventId);
    writeStoredIds(PENDING_DISMISS_STORAGE_KEY, [...pendingDismissals]);
  }

  function forgetPendingDismissal(eventId) {
    pendingDismissals.delete(eventId);
    writeStoredIds(PENDING_DISMISS_STORAGE_KEY, [...pendingDismissals]);
  }

  async function flushPendingDismissals() {
    for (const eventId of [...pendingDismissals]) {
      try {
        await callTool('cove_bridge_dismissed', { eventId });
        forgetPendingDismissal(eventId);
      } catch (error) {
        const message = error && error.message ? error.message : String(error);
        if (message.includes('Unknown event')) {
          forgetPendingDismissal(eventId);
          continue;
        }
        return false;
      }
    }
    return true;
  }

  async function flushPendingAcks() {
    for (const eventId of [...pendingAcks]) {
      try {
        await callTool('cove_bridge_delivered', { eventId });
        forgetPendingAck(eventId);
      } catch (error) {
        const message = error && error.message ? error.message : String(error);
        if (message.includes('Unknown event')) {
          forgetPendingAck(eventId);
          continue;
        }
        return false;
      }
    }
    return true;
  }

  async function flushPendingTerminals() {
    if (!await flushPendingDismissals()) return false;
    return await flushPendingAcks();
  }

  function notify(method, params) {
    window.parent.postMessage({ jsonrpc: '2.0', method, params }, '*');
  }

  function request(method, params) {
    const id = ++rpcId;
    window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      window.setTimeout(() => {
        const entry = pending.get(id);
        if (!entry) return;
        pending.delete(id);
        reject(new Error(method + ' timed out'));
      }, 18000);
    });
  }

  function requestHumanDecision(method, params) {
    const id = ++rpcId;
    window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      window.setTimeout(() => {
        if (!pending.has(id)) return;
        setStatus('等待你确认发送或取消；不会重复投递。');
      }, 18000);
    });
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== '2.0') return;
    if (message.id === undefined) return;
    const entry = pending.get(message.id);
    if (!entry) return;
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(message.error.message || 'Host request failed'));
    else entry.resolve(message.result);
  }, { passive: true });

  async function initialize() {
    await request('ui/initialize', {
      appInfo: { name: 'cove-bridge-widget', version: '0.1.0' },
      appCapabilities: {},
      protocolVersion: '2026-01-26'
    });
    notify('ui/notifications/initialized', {});
    bridgeReady = true;
    toggleEl.disabled = false;
    toggleEl.textContent = '开始监听';
    setStatus('已挂载，尚未监听。');
  }

  async function callTool(name, args) {
    if (!bridgeReady) throw new Error('Bridge is not initialized');
    return request('tools/call', { name, arguments: args || {} });
  }

  const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

  function setIdleStatus() {
    if (!listening) return;
    setStatus(sseConnected
      ? 'SSE 实时监听中，暂无新事件。'
      : '监听中（SSE 重连中，60 秒轮询兜底）。');
  }

  async function openWakeStream(generation) {
    let retryMs = 1000;

    while (listening && generation === streamGeneration) {
      try {
        const sessionResult = await callTool('cove_bridge_listener_session', {});
        const session = sessionResult && sessionResult.structuredContent;
        if (!session || !session.token || !session.streamUrl) {
          throw new Error('Bridge did not return a wake-stream session');
        }

        const streamUrl = new URL(String(session.streamUrl));
        streamUrl.searchParams.set('session', String(session.token));

        await new Promise((resolve, reject) => {
          const source = new EventSource(streamUrl.toString());
          streamAbort = { abort: () => source.close() };

          const fail = () => {
            source.close();
            reject(new Error('EventSource disconnected'));
          };

          source.onopen = () => {
            sseConnected = true;
            retryMs = 1000;
            setStatus('SSE 实时监听中。');
          };

          source.addEventListener('wake', () => {
            void syncOnce();
          });

          source.addEventListener('session-expired', () => {
            source.close();
            resolve();
          });

          source.onerror = fail;
        });
      } catch (error) {
        if (!listening || generation !== streamGeneration) return;
        const message = error && error.message ? error.message : String(error);
        setStatus('SSE 暂时断开：' + message + '；60 秒轮询兜底，正在重连…');
      } finally {
        if (generation === streamGeneration) {
          sseConnected = false;
          streamAbort = null;
        }
      }

      if (!listening || generation !== streamGeneration) return;
      await sleep(retryMs);
      retryMs = Math.min(15_000, retryMs * 2);
    }
  }

  async function dispatch(event) {
    await request('ui/update-model-context', {
      content: [{
        type: 'text',
        text: String(event.modelContext || ''),
        annotations: { audience: ['assistant'], priority: 1 }
      }],
      structuredContent: {
        bridgeEvent: {
          eventId: String(event.id),
          correlationId: String(event.correlationId || event.id),
          kind: String(event.kind || ''),
          source: String(event.source || ''),
          stream: String(event.stream || ''),
          stateKey: String(event.stateKey || ''),
          replyRoute: String(event.replyRoute || ''),
          replyPolicy: String(event.replyPolicy || ''),
          createdAt: String(event.createdAt || '')
        }
      }
    });

    // ui/message is now a human-confirmed Host operation. Once handed off,
    // never release the event merely because the user takes longer than an
    // ordinary RPC timeout to decide.
    setStatus('已交给 ChatGPT，等待你确认发送或取消…');
    try {
      await requestHumanDecision('ui/message', {
        role: 'user',
        content: [{ type: 'text', text: String(event.visibleText || '') }]
      });
      return { outcome: 'sent' };
    } catch (error) {
      return { outcome: 'dismissed', error };
    }
  }

  async function syncOnce() {
    if (!listening || inFlight) return;
    inFlight = true;
    let shouldContinue = false;
    try {
      const terminalsFlushed = await flushPendingTerminals();
      if (!terminalsFlushed) {
        setStatus('事件终态正在重试确认；不会重复显示。');
        return;
      }

      const result = await callTool('cove_bridge_sync', {});
      const event = result && result._meta && result._meta.event;
      if (!event) {
        const state = result && result.structuredContent;
        if (state && state.awaitingReply) {
          setStatus('等待当前消息完成回传…');
        } else {
          setIdleStatus();
        }
        return;
      }

      const eventId = String(event.id);
      if (recentlyDispatched.has(eventId)) {
        rememberPendingAck(eventId);
        const acked = await flushPendingAcks();
        if (!acked) {
          setStatus('重复事件已拦截，正在重试送达确认。');
          return;
        }
        shouldContinue = true;
        setStatus('重复投递已拦截。');
        return;
      }

      setStatus('正在投递事件…');
      let dispatchResult;
      try {
        dispatchResult = await dispatch(event);
      } catch (error) {
        // Only failures before ui/message handoff are safe to retry.
        await callTool('cove_bridge_release', { eventId }).catch(() => {});
        throw error;
      }

      // ui/message has been handed to the Host. Persist this before terminal
      // acknowledgement so a widget reload cannot show the same prompt again.
      rememberDispatched(eventId);

      if (dispatchResult && dispatchResult.outcome === 'dismissed') {
        rememberPendingDismissal(eventId);
        const dismissed = await flushPendingTerminals();
        if (!dismissed) {
          setStatus('本次提示已取消；正在重试取消确认，不会重复显示。');
          return;
        }
        shouldContinue = true;
        setStatus('本次提示已取消，不会重复显示。');
        return;
      }

      rememberPendingAck(eventId);
      const acked = await flushPendingTerminals();
      if (!acked) {
        setStatus('事件已显示，正在重试送达确认；不会重复显示。');
        return;
      }

      shouldContinue = true;
      setStatus('事件已送达。');
    } catch (error) {
      setStatus('监听错误：' + (error && error.message ? error.message : String(error)));
    } finally {
      inFlight = false;
      if (listening && shouldContinue) {
        window.setTimeout(() => void syncOnce(), 0);
      }
    }
  }

  function scheduleFallback() {
    window.clearInterval(timer);
    if (!listening) return;
    timer = window.setInterval(() => void syncOnce(), FALLBACK_POLL_MS);
  }

  function startListening() {
    listening = true;
    toggleEl.textContent = '停止监听';
    setStatus('正在建立 SSE 实时监听…');
    scheduleFallback();
    streamGeneration += 1;
    const generation = streamGeneration;
    void openWakeStream(generation);
    void syncOnce();
  }

  function stopListening() {
    listening = false;
    toggleEl.textContent = '开始监听';
    window.clearInterval(timer);
    streamGeneration += 1;
    sseConnected = false;
    if (streamAbort) streamAbort.abort();
    streamAbort = null;
    setStatus('已暂停。');
  }

  toggleEl.addEventListener('click', () => {
    if (listening) stopListening();
    else startListening();
  });

  initialize().catch((error) => {
    toggleEl.disabled = true;
    toggleEl.textContent = '连接失败';
    setStatus('组件连接失败：' + (error && error.message ? error.message : String(error)));
  });
})();
</script>
</body>
</html>`;
}
