# FoggyGramm

Open-source, self-hosted multi-account Telegram client with a monochrome
glassmorphism UI. Run your own copy: one admin login unlocks an encrypted
vault holding all your Telegram accounts — no re-adding accounts one by one
after reinstalls.

![License: MIT](https://img.shields.io/badge/License-MIT-black.svg)

## Requirements
- Windows (tested), Linux/macOS (should work), Python 3.12
- Your own free API ID + API hash from https://my.telegram.org
  (API development tools). Each install uses its owner's keys.

## Run
```powershell
cd telegram-hub
.\run.ps1
```
Then open http://127.0.0.1:8765

On Linux/macOS:
```sh
cd telegram-hub
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

The first page is **setup**: paste your API ID + API hash and choose an
admin username/password to create your vault. After that, log in and click
**Add account** to add each Telegram account once (Telegram sends a code;
enter it, plus your 2FA password if you have one). From then on, logging in
restores every account with no codes.

## Roles
- **Admin** — sees and auto-restores *all* accounts.
- **Regular** — created by the admin; sees only 1–2 assigned accounts, for a
  clean view. Regular users can only log in while the admin has the server
  unlocked.

## Backup & sync
- **Backup modes** (admin → Sync tab): `manual`, `on-change` (after every
  account/user change), or `daily` (background check, at most once a day).
  Old backups are pruned automatically (keep last N, default 10).
- **Restore** brings a backup back; your current vault is always kept first
  as a `pre-restore-…` safety backup, so nothing is ever lost.
- **Sync folder** (`data/sync/`): Export copies the encrypted vault there —
  point Syncthing/Drive at that folder to carry it to your other devices.
  Import brings it back, again keeping a `pre-import-…` safety backup first.

## Where your data lives (never committed)
- `data/vault.json` — non-secret metadata (salt, admin name). Safe to sync.
- `data/accounts.enc` — **encrypted** accounts + sessions + users. Ciphertext only.
- `data/telegram_api.json` — **your private API keys.** Git-ignored, never published.
- `data/backups/` — encrypted vault snapshots + manifests.
- `data/sync/` — the encrypted copy to sync between devices.
- `data/settings.json` — backup mode, keep-limit, device name.

A session string equals full control of an account, so it is never stored
unencrypted.

## Security notes
- Use a strong admin password. PBKDF2-SHA256 (600k iterations) protects the vault.
- If you lose the admin password, the sessions are unrecoverable by design.
- Never commit `data/` or `.env` — `.gitignore` already excludes them.
- Respect Telegram's Terms of Service. Automating spam bans accounts.

## Contributing
Issues and pull requests are welcome.
- Roadmap: M3 media/chat backup + cloud saver; M4 bots; M5 groups; M6 AI
  agent; M7 UI polish; M8 `.exe`; M9 `.apk`.
- Keep PRs small, match the existing code style, and never include real API
  keys, session strings, or anything from your `data/` folder.

## License
MIT — see [LICENSE](LICENSE).
