# Cove Companion for AstrBot 1.0

[中文](README.md)

> Unofficial project. It does not include or redistribute NetEase Cloud Music logos, songs, artwork, lyrics, cookies, or credentials. Artwork and lyrics are fetched from external services at runtime.

A single-owner AstrBot companion for NetEase Cloud Music Listen Together. The room agent uses the owner's current AstrBot persona, provider, recent QQ context, and recalled long-term memory.

![Desktop player](assets/screenshots/overview.png)

| Mobile player | Queue drawer |
| --- | --- |
| ![Mobile player](assets/screenshots/mobile.png) | ![Queue drawer](assets/screenshots/mobile-queue.png) |

[Mobile player · more actions](assets/screenshots/mobile-more.png)

## Features

- Original player-first console with ambient artwork, smooth progress, synced lyrics, detailed queue, and operation receipts.
- SSE realtime state updates with polling fallback for older AstrBot versions.
- Search, enqueue, play now, pause/resume, skip, recommend, and cancel recommendation through verified snapshots and realtime receipts.
- User room messages take priority over scheduled recommendations and proactive expression.
- Default 30-day retention and seven daily backups.
- Memory summaries are written per song or session, not per chat turn.

## Requirements

- AstrBot >= 4.27.0
- A tool-capable chat provider
- Cove Resonance with a valid NetEase session
- Linux or Docker; Windows/macOS are community-supported only

Apply `cove_patch/astrbot-cove-v1.patch` to Cove baseline `e63cfa5`. The plugin does not work without the Cove bridge.

## Install

See the Chinese README for full Linux and Docker steps. In short:

```bash
python -m unittest discover -s astrbot_plugin_cove_companion/tests -v
cd astrbot_plugin_cove_companion/frontend
npm ci && npm test && npm run build
```

Place the plugin in AstrBot's plugin directory, configure the owner and Cove token path, then run the first-use checks in the browser.

## Privacy and Safety

- The browser never receives the Cove token, cookies, full owner UMO, or NetEase UID.
- Diagnostic bundles omit chat bodies and credentials.
- The plugin does not expose queue deletion, queue clearing, or member removal.
- No telemetry is collected.

## License

MIT. This project is not affiliated with or endorsed by NetEase Cloud Music.
