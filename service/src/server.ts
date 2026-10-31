import { randomUUID } from "node:crypto";
import { chmodSync, existsSync, unlinkSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { createMcpServer } from "./mcp.js";
import { buildBridgeEvent } from "./bridge/events.js";
export { buildBridgeEvent } from "./bridge/events.js";
import { MCP_PATHS } from "./profiles.js";
import { listenerWakeHub } from "./listenerWake.js";
import { PlaybackStateStore } from "./netease/playbackState.js";
import { createTogetherWorker } from "./netease/togetherWorker.js";
import { InMemoryEventQueue } from "./queue.js";
import { AstrbotHttpRouter } from "./astrbot/http.js";
import { AstrbotCoveService } from "./astrbot/service.js";
import { CredentialVault, MusicAuthentication } from "./astrbot/credentials.js";
import { AstrbotCoveStore } from "./astrbot/store.js";

const PORT = Number(process.env.PORT ?? 8787);
const BIND_HOST = process.env.BIND_HOST?.trim() || "0.0.0.0";
const INGEST_TOKEN = process.env.BRIDGE_INGEST_TOKEN ?? "";
const UNIX_SOCKET = process.env.BRIDGE_UNIX_SOCKET?.trim() ?? "";
const HOST_MODE = process.env.HOST_MODE?.trim().toLowerCase() ?? "chatgpt";
const queue = new InMemoryEventQueue();
const playbackState = new PlaybackStateStore();

function enqueueTogetherEvent(
  source: string,
  text: string,
  additionalModelContext?: string,
): boolean {
  if (HOST_MODE === "astrbot") return false;
  const id = randomUUID();
  const created = queue.enqueue(buildBridgeEvent(
    id,
    source,
    text,
    new Date().toISOString(),
    additionalModelContext,
  ));
  if (created) listenerWakeHub.wake(source);
  return created;
}

const astrbotStore = new AstrbotCoveStore(
  process.env.ASTROBOT_COVE_DB?.trim()
    || (process.env.NODE_ENV === "test" ? ":memory:" : "data/astrbot-cove.sqlite3"),
);
const astrbotService = new AstrbotCoveService(astrbotStore);
const musicAuth = new MusicAuthentication(new CredentialVault());
const activeCookie = musicAuth.bootstrap(process.env.NETEASE_COOKIE?.trim() || "");
process.env.NETEASE_COOKIE=activeCookie;
let togetherWorker = createTogetherWorker(
  enqueueTogetherEvent,
  playbackState,
  (event) => astrbotService.ingestWorkerEvent(event),
);
astrbotService.attachWorker(togetherWorker);
astrbotService.authentication=musicAuth;
astrbotService.replaceWorker=async(cookie)=>{
  await togetherWorker.stop();
  togetherWorker=createTogetherWorker(enqueueTogetherEvent,playbackState,(event)=>astrbotService.ingestWorkerEvent(event),cookie);
  astrbotService.attachWorker(togetherWorker);process.env.NETEASE_COOKIE=cookie;togetherWorker.start();
};
const astrbotRouter = new AstrbotHttpRouter(astrbotService);

const eventInput = z.object({
  eventId: z.string().trim().min(1).max(200).optional(),
  source: z.string().trim().min(1).max(80).default("test"),
  text: z.string().trim().min(1).max(4000),
});

function writeJson(res: ServerResponse, status: number, value: unknown): void {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(value));
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > 64 * 1024) throw new Error("request_too_large");
    chunks.push(buffer);
  }
  const body = Buffer.concat(chunks).toString("utf8");
  return body ? JSON.parse(body) : {};
}

function authorized(req: IncomingMessage): boolean {
  return !INGEST_TOKEN || req.headers.authorization === `Bearer ${INGEST_TOKEN}`;
}

export function createHttpServer() {
  return createServer(async (req, res) => {
    if (!req.url || !req.method) {
      res.writeHead(400).end("Bad Request");
      return;
    }
    const url = new URL(req.url, `http://${req.headers.host ?? "localhost"}`);

    if (await astrbotRouter.handle(req, res, url)) return;

    if (req.method === "OPTIONS") {
      res.writeHead(204, {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "content-type, authorization, mcp-session-id",
        "Access-Control-Expose-Headers": "Mcp-Session-Id",
      });
      res.end();
      return;
    }

    if (req.method === "GET" && url.pathname === "/") {
      writeJson(res, 200, {
        ok: true,
        service: "cove-bridge",
        version: "0.1.0",
        queue: queue.status(),
        listener: listenerWakeHub.status(),
      });
      return;
    }

    if (req.method === "GET" && url.pathname === "/listener/events") {
      const authorization = req.headers.authorization ?? "";
      const bearerToken = authorization.startsWith("Bearer ")
        ? authorization.slice("Bearer ".length).trim()
        : "";
      const token = bearerToken || url.searchParams.get("session")?.trim() || "";
      const session = token ? listenerWakeHub.consumeSession(token) : null;
      if (!session) {
        writeJson(res, 401, { error: "invalid_or_expired_listener_session" });
        return;
      }
      listenerWakeHub.subscribe(res, session.expiresAtMs);
      return;
    }

    if (req.method === "POST" && url.pathname === "/events") {
      if (!authorized(req)) {
        writeJson(res, 401, { error: "unauthorized" });
        return;
      }
      try {
        const parsed = eventInput.safeParse(await readJson(req));
        if (!parsed.success) {
          writeJson(res, 400, { error: parsed.error.flatten() });
          return;
        }
        const createdAt = new Date().toISOString();
        const id = parsed.data.eventId ?? randomUUID();
        const event = buildBridgeEvent(id, parsed.data.source, parsed.data.text, createdAt);
        const created = queue.enqueue(event);
        if (created) listenerWakeHub.wake(parsed.data.source);
        writeJson(res, created ? 201 : 200, { ok: true, created, eventId: id });
      } catch (error) {
        const message = error instanceof Error ? error.message : "invalid_request";
        writeJson(res, message === "request_too_large" ? 413 : 400, { error: message });
      }
      return;
    }

    const mcpMethods = new Set(["POST", "GET", "DELETE"]);
    const mcpProfile = MCP_PATHS.get(url.pathname);
    if (mcpProfile && mcpMethods.has(req.method)) {
      res.setHeader("Access-Control-Allow-Origin", "*");
      res.setHeader("Access-Control-Expose-Headers", "Mcp-Session-Id");
      const server = createMcpServer(queue, playbackState, togetherWorker, mcpProfile);
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
        enableJsonResponse: true,
      });
      res.on("close", () => {
        void transport.close();
        void server.close();
      });
      try {
        await server.connect(transport);
        await transport.handleRequest(req, res);
      } catch (error) {
        console.error("MCP request failed", error);
        if (!res.headersSent) writeJson(res, 500, { error: "internal_server_error" });
      }
      return;
    }

    res.writeHead(404).end("Not Found");
  });
}

if (process.env.NODE_ENV !== "test") {
  createHttpServer().listen(PORT, BIND_HOST, () => {
    console.log("Cove Resonance listening on http://" + BIND_HOST + ":" + PORT + " (MCP: /mcp, /mcp/music; AstrBot: /astrbot/v1)");
    astrbotService.start();
    togetherWorker.start();
  });

  if (UNIX_SOCKET) {
    if (existsSync(UNIX_SOCKET)) unlinkSync(UNIX_SOCKET);
    createHttpServer().listen(UNIX_SOCKET, () => {
      chmodSync(UNIX_SOCKET, 0o666);
      console.log("Cove Resonance listening on unix://" + UNIX_SOCKET + " (MCP: /mcp, /mcp/music)");
    });
  }
}
