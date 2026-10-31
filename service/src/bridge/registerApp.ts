import { registerAppResource, registerAppTool, RESOURCE_MIME_TYPE } from "@modelcontextprotocol/ext-apps/server";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { buildListenerHtml } from "../listener-html.js";
import { listenerWakeHub } from "../listenerWake.js";

export const RESOURCE_URI = "ui://widget/cove-bridge.html";
const PUBLIC_ORIGIN = process.env.BRIDGE_PUBLIC_ORIGIN?.trim()
  || "http://localhost:" + (process.env.PORT ?? "8787");

export function registerBridgeApp(server: McpServer): void {
  const html = buildListenerHtml();

  registerAppResource(
    server,
    "cove-bridge-widget",
    RESOURCE_URI,
    {},
    async () => ({
      contents: [{
        uri: RESOURCE_URI,
        mimeType: RESOURCE_MIME_TYPE,
        text: html,
        _meta: {
          ui: {
            domain: PUBLIC_ORIGIN,
            prefersBorder: false,
            csp: { connectDomains: [PUBLIC_ORIGIN], resourceDomains: [] },
          },
          "openai/widgetDescription": "A manually controlled listener for Cove Resonance events.",
        },
      }],
    }),
  );

  registerAppTool(
    server,
    "open_cove_bridge",
    {
      title: "Open Cove Resonance",
      description: "Mount the Cove Resonance listener component in an idle state. Only use when the user explicitly asks to open it. Opening does not start listening; the user starts listening from the component.",
      inputSchema: {},
      outputSchema: { ready: z.boolean(), listening: z.boolean() },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        openWorldHint: false,
      },
      _meta: {
        ui: { resourceUri: RESOURCE_URI },
        "openai/outputTemplate": RESOURCE_URI,
      },
    },
    async () => ({
      structuredContent: { ready: true, listening: false },
      content: [{ type: "text", text: "Cove Resonance mounted in an idle state. Listening has not started." }],
    }),
  );

  server.registerTool(
    "cove_bridge_listener_session",
    {
      title: "Cove Resonance listener session",
      description: "Create a short-lived, single-use authorization token for the Cove Resonance wake stream. App-only.",
      inputSchema: {},
      outputSchema: {
        token: z.string(),
        streamUrl: z.string(),
        expiresAt: z.string(),
        fallbackPollMs: z.number(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: false,
        idempotentHint: false,
      },
      _meta: { ui: { visibility: ["app"] } },
    },
    async () => {
      const session = listenerWakeHub.createSession();
      return {
        structuredContent: {
          token: session.token,
          streamUrl: PUBLIC_ORIGIN + "/listener/events",
          expiresAt: session.expiresAt,
          fallbackPollMs: 60_000,
        },
        content: [],
      };
    },
  );
}
