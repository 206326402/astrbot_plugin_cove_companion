type Bridge = {
  ready: () => Promise<unknown>;
  apiGet?: (endpoint: string, params: unknown) => Promise<unknown>;
  apiPost?: (endpoint: string, body: unknown) => Promise<unknown>;
  subscribeSSE?: (endpoint: string, handlers: unknown, params: unknown) => Promise<string>;
  unsubscribeSSE?: (subscriptionId: string) => Promise<unknown>;
  api?: {get: (endpoint: string, options?: unknown) => Promise<unknown>; post: (endpoint: string, options?: unknown) => Promise<unknown>} | ((endpoint: string, options: unknown) => Promise<unknown>);
};
declare global { interface Window { AstrBotPluginView?: Bridge; AstrBotPluginPage?: Bridge; } }

export class BridgeError extends Error {
  constructor(message: string, public readonly phase: string) { super(message); }
}
const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
let pending: Promise<Bridge> | undefined;

export async function connect(): Promise<Bridge> {
  if (!pending) pending = (async () => {
    const started = Date.now();
    let bridge: Bridge | undefined;
    while (Date.now() - started < 12_000) {
      bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
      if (bridge) break;
      await delay(80);
    }
    if (!bridge) throw new BridgeError('未检测到 AstrBot 页面桥接。请从插件页面重新打开。', 'missing');
    await Promise.race([bridge.ready(), delay(10_000).then(() => { throw new BridgeError('AstrBot 页面握手超时。', 'timeout'); })]);
    return bridge;
  })().catch((error) => { pending = undefined; throw error; });
  return pending;
}

export async function api<T>(endpoint: string, method: 'GET' | 'POST' = 'GET', params: Record<string, unknown> = {}): Promise<T> {
  const bridge = await connect();
  const clean = Object.fromEntries(Object.entries(params).filter(([, value]) => value !== undefined && (method === 'POST' || (value !== null && value !== ''))));
  const options = method === 'GET' ? {query: clean} : {body: clean};
  const call = method === 'GET' && bridge.apiGet
    ? bridge.apiGet(endpoint, clean)
    : method === 'POST' && bridge.apiPost
      ? bridge.apiPost(endpoint, clean)
      : typeof bridge.api === 'function'
        ? bridge.api(endpoint, {method, ...options})
        : method === 'GET'
          ? bridge.api!.get(endpoint, options)
          : bridge.api!.post(endpoint, options);
  const timeout = method === 'POST' ? 75_000 : 20_000;
  const response = await Promise.race([call, delay(timeout).then(() => { throw new BridgeError(method === 'POST' ? '仍未收到操作结果，请先查看活动与回执，避免重复发送。' : '读取超时，请重试。', 'request'); })]) as {status?: string; message?: string; data?: unknown; error?: string};
  if (response?.status === 'error' || response?.error) throw new BridgeError(response.message || response.error || '接口请求失败', 'request');
  return (response?.status === 'ok' || response?.status === 'success' ? response.data : response) as T;
}

export const get = <T>(endpoint: string, params: Record<string, unknown> = {}) => api<T>('v1/' + endpoint, 'GET', params);
export const post = <T>(endpoint: string, body: Record<string, unknown>) => api<T>('v1/' + endpoint, 'POST', body);
export const idempotencyKey = () => 'ui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);

export type StreamMessage = {raw?: string; parsed?: unknown; eventType?: string; lastEventId?: string};

export async function subscribeStream(
  endpoint: string,
  onMessage: (message: StreamMessage) => void,
  handlers: {onOpen?: () => void; onError?: (error?: unknown) => void} = {},
): Promise<() => void> {
  const bridge = await connect();
  if (!bridge.subscribeSSE || !bridge.unsubscribeSSE) throw new BridgeError('当前 AstrBot 不支持 SSE 实时推送。', 'unsupported');
  const subscriptionId = await bridge.subscribeSSE(
    endpoint,
    {onMessage, onOpen: handlers.onOpen, onError: handlers.onError},
    {},
  );
  return () => { void bridge.unsubscribeSSE?.(subscriptionId); };
}
