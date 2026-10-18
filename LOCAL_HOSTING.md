# Running the tracker on your laptop

Instead of Railway, the app can run on your own computer. Your phone (Overland)
and anyone viewing the site reach it through **Tailscale Funnel**, which gives
the laptop a permanent public HTTPS address - free, and visitors don't need to
install anything.

```
phone (Overland) --HTTPS--> https://<laptop>.<tailnet>.ts.net --Funnel--> this laptop :8080 --> MySQL (JawsDB), Discord
```

## One-time setup

1. **Install Tailscale** and sign in (any Google / Microsoft / GitHub account):
   ```
   winget install --id tailscale.tailscale
   ```
2. **Turn on Funnel for port 8080.** The first time, it opens a page to approve
   enabling HTTPS and Funnel. `--bg` keeps it on, even after a reboot:
   ```
   tailscale funnel --bg 8080
   ```
   It prints your public address, like `https://your-laptop.tail1234.ts.net`.
   (`tailscale funnel status` shows it again; `tailscale funnel --bg 8080 off` turns it off.)
3. **Add these to `.env`** (alongside what's already there):
   ```
   SETTINGS_SCOPE=production
   RECOVER_ACTIVE_RUNS=true
   PUBLIC_SITE_URL=https://your-laptop.tail1234.ts.net
   SIX_SEVEN_DISCORD_ID=        (your friend's ID from Railway, if you use the 6.7 DM)
   ```
   - `SETTINGS_SCOPE=production` keeps the goal, target pace and switches you set on Railway.
   - `RECOVER_ACTIVE_RUNS=true` lets a run in progress survive restarting the server.
   - Make sure `OVERLAND_ACCESS_TOKEN` matches the token Overland sends (Overland's settings).
4. **Point Overland at the laptop:** change its receiver endpoint from the Railway
   address to `https://your-laptop.tail1234.ts.net/overland` (same token).
5. **Stop Windows from sleeping while plugged in** - Settings > System > Power:
   set sleep to *Never* when plugged in, and set closing the lid to *Do nothing*
   (Control Panel > Power Options > Choose what closing the lid does). The server
   also asks Windows to stay awake while it runs, but closing the lid overrides that.

## Every time

Double-click **`start_local.bat`** (or run `venv\Scripts\python serve_local.py`)
and leave the window open. It checks `.env` first and tells you what's missing.

Quick check: on your phone, turn Wi-Fi off and open your `ts.net` address - the
dashboard should load.

## Good to know

- **If the laptop is off, asleep or offline during a run**, Overland keeps the
  points on your phone and sends them once it can reach the laptop again. Nothing
  is lost, but you won't get live DMs until then (and the missed ones arrive together).
- **Restarting the server mid-run is fine** with `RECOVER_ACTIVE_RUNS=true` - the
  run is rebuilt from what's saved in the database.
- **Only run one server at a time.** If you ever bring Railway back, stop this one
  (they share the database).
- The site is public at the `ts.net` address, just as it was on Railway: the admin
  password protects the admin panel, and the Overland token protects run uploads.
