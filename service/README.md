# Cove Companion service

This directory contains the optional Cove Resonance companion service. It requires Node.js 22.x and a locally protected token. It does not ship credentials, runtime data, cookies, or a database.

For Linux installation from the repository root:

```bash
bash scripts/install-cove.sh
bash scripts/check-cove.sh
```

The service listens on `127.0.0.1:8787` by default. Keep it bound to localhost unless you have a deliberate reverse proxy and an independent access policy. See the root README for privacy, backup, update, and uninstall guidance.
