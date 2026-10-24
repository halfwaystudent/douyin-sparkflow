# Changelog

## 2026-10-24

- Added group-chat targets bound to conversation IDs (PR #45): the picker shows `name（full id）`; duplicate names, incomplete scans, missing bindings, or a changed target fail closed instead of sending. Group-list refresh is read-only and patrol now checks group targets by their bound IDs.
- Added favicon support and a redirect from the legacy send-console anchor.
- Added strict protobuf `message_send` receipt parsing. A send is confirmed only for an unambiguous exact success; repeated decision fields, unknown fields, non-zero integer check codes at any JSON depth, malformed or oversized checks, and every other uncertain response stay unverified and are never automatically resent.
- Added Web-console login protection: five failures per IP-and-username in 15 minutes lock that pair for 15 minutes; after ten failures for one username across IPs, attempts slow to one per 30 seconds. Tracking is bounded, with no IP-wide lock. New passwords require at least 10 characters, two character classes, and no username; existing passwords remain valid.
- Added `deploy/release-files.sh` for drift-checked, backed-up, hash-verified releases with automatic rollback and a recorded release version.
- Normalized tracked text files to LF with `.gitattributes`, added Python 3.10 and 3.11 CI coverage, Dependabot configuration, and load-independent cross-process lock tests.

## 2026-10-13

- Added run-result push notifications (Telegram, DingTalk with signing, WeCom, generic JSON webhook): immediate failure notices merged per run and deduplicated per account/target/category per day, plus a once-per-day summary. Configured by admins in the settings page; secrets are shown masked and never logged.

## 2026-08-27

- Changed the project license for project-owned code from MIT License to PolyForm Noncommercial License 1.0.0. Future versions are source-available for noncommercial use only; prior versions remain governed by their applicable license terms.
- Added prominent non-official project, platform-risk, account-authorization, data-responsibility, commercial-use, and intellectual-property notices to the README files.

## 2026-08-25

- Made the experimental branch default to direct Douyin business traffic; Mihomo is now an explicit advanced option for browser traffic.

- Made login-browser networking direct-first with Mihomo fallback and explicit preflight errors.

## 2026-07-11

- Reduced login-desktop resource spikes with lazy Chromium startup, idle/after-export shutdown, status caching, reduced-motion page styling, lower-frequency x11vnc polling, and Compose CPU/memory/PID limits.

- Changed QR refresh from re-reading an expired image to forcing the Douyin login page to generate a new cache-busted QR code, and stopped serving expired QR screenshots.

- Fixed a blank Douyin login desktop by preventing build-time localhost proxy variables from leaking into runtime Chromium and explicitly wiring lowercase/uppercase runtime proxy variables.

- Added an authenticated same-origin noVNC HTTP/WebSocket proxy and synchronous mobile popup handling so login tasks work from phones without exposing port 8788 publicly.

- Fixed the server image to provision Node.js 22 explicitly for protocol mode and excluded runtime logs/configuration from the Docker build context.

- Reworked the Web console into a responsive unified operations dashboard with local Lucide icons, clearer account/target status, and safer confirmation dialogs.
- Added strong send-confirmation state handling, retryable unconfirmed targets, failure categorization, account cooldowns, and focused manual retry modes.
- Improved friend-list discovery, persistent browser profiles, Cookie synchronization, and browser/protocol failure persistence.
- Expanded the login desktop service with more robust Douyin identity export and bounded diagnostic endpoints.
- Added authenticated no-cache overview data that excludes message contents, Cookie data, receipts, and server credentials.
- Added unit coverage for send-state behavior, stale-lock safety, overview redaction, authentication, and public settings.
- Made stale-lock PID probing portable across Linux and Windows and sanitized the public configuration template.
- Removed obsolete standalone account, login-workspace, and settings templates now consolidated into the dashboard.
- Fixed standard Compose scheduling by sharing persistent browser profiles with scheduler/task services, while limiting host Docker access to services that actually need it.
- Moved the GitHub Actions workflow to the repository root, corrected its working directory/tests/profile/artifact paths, and pinned actions to commit SHAs.
- Split the tracked `config.example.json` template from the ignored runtime `config.json`, with update-time config preservation.
- Made login-desktop API/public URLs honor container environment settings and synchronize the configured schedule at Web startup.
- Bound noVNC and Mihomo host ports to loopback by default and documented SSH-tunnel access.
- Added an explicit Node.js image build check for protocol mode, made the cron reader tolerate Windows UTF-8 BOM files, and removed unused legacy login-session, relogin-worker, and nested Compose entrypoints.

## 2026-05-30

- Redesigned the Web UI into dedicated overview, login workspace, accounts, send console, logs, and settings views.
- Moved the console styling and interaction scripts into `DouYinSparkFlow/webui/static/` with a denser admin-console layout and mobile drawer navigation.
- Added safer confirmations for manual operations and localized the main Web UI feedback messages.
- Removed the redundant send-console navigation label while keeping send details available from contextual actions.
- Added a persistent sun/moon toggle for switching between light and dark console themes.
- Updated the README preview image and added a screenshot-based usage guide.
- Made the screenshot usage guide entry more visible in the README.
- Added a project disclaimer to the root README.
- Added a Linux Do friendly link and GitHub star history chart.
- Refined the root README wording and structure for an open source project style.
- Reordered the disclaimer and star history chart, and refined runtime file wording.
- Added upstream project attribution and emoji-styled README sections.
- Added a masked UI preview image to the root README.
