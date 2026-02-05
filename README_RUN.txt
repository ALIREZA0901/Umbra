Umbra v2 - Final ZIP (Phase 1: UI + Apply System)

Folder Structure (after extract):
- main.py
- style.css
- requirements.txt
- configs/settings.json
- core/
- ui/
- logs/
- runtime/

How to install & run (Windows):
1) Open CMD in this folder (same level as main.py)
2) Create venv (recommended):
   py -3.12 -m venv venv
   venv\Scripts\activate
3) Install deps:
   pip install -r requirements.txt
4) Run:
   python main.py

Notes:
- Advanced speed test (Download/Upload) ONLY runs when you click it and confirm.
- Engine Start/Stop is fixed: Stop Engine stops all threads; Exit closes fully.
- System tray behavior: Settings -> Behavior (minimize to tray or exit).
- Auto refresh: Settings -> Behavior (interval + pause when minimized).
- App Launcher: add apps, detect running apps, group/move them, track last launch, enable/disable, launch/stop selected, enabled, or group, and refresh running status.
- DNS list is prefilled (IR + GLOBAL). Add/remove in Settings -> DNS.
- VPN manager supports: import from clipboard/paste, subscriptions, auto-detect type/core (for management). (Core-runner integration is planned for next phase.)

New in this build:
- First Run Wizard: on first start, choose Copilot intensity (Basic/Helpful/Expert) and optional safe DNS ping check.
- Copilot panel on Dashboard: manual "Run Analysis" (no background tests) + Recipes + Rollback last Apply.
- Settings -> Behavior: Copilot mode selector + Re-run First-Run Setup + Rollback.
- Core Updater now keeps backups under cores/_backups/ before replacing binaries.
