# Helpdesk

A local-first, Freshdesk-style ticketing system. Pure Python 3 standard library + SQLite - nothing to install.

**Start:** double-click `start.bat` (or `python app.py`). Opens http://localhost:8765/.
Agent console at `/`, customer portal at `/portal`. Data is stored in `%LOCALAPPDATA%\Helpdesk\helpdesk.db` on Windows (local disk, because SQLite
should not live on a network share) or next to the app elsewhere. Demo data is seeded on first run; delete the file to reset.

## What it does
- Tickets with status (Open/Pending/Resolved/Closed), priority, agent, group, type, tags, search and views
  (My open, Unassigned, Overdue, ...)
- Threaded conversation: customer messages, public replies, private notes, canned replies, activity log
- SLA timers per priority (first response + resolution), overdue flags, dashboard
- Email in: IMAP polling (threads by Message-ID / In-Reply-To / `[#id]` in subject, strips quoted text, dedupes)
- Email out: SMTP replies and optional notifications via your mailboxes (otherwise replies are saved and marked "not sent")
- Automations: rules on ticket created/updated with conditions and actions (set field, add tag)
- Knowledge base + public customer portal (article search, submit a request)
- Settings: add / edit / delete agents (deleting an agent unassigns their tickets; the last agent can't be deleted)
- WhatsApp: save a customer's number on the ticket; one tap opens WhatsApp with the ticket info pre-filled (to the customer, or
  to any contact you pick). Local numbers starting with 0 use the country code set in Settings (default 27).
- Email a ticket via your default mail app (Outlook): to the customer, or blank To: to forward it
- Mobile friendly: phone layout with back button, chip filters and a + button; installable as a home-screen app
  (to open it from a phone, run with `--host 0.0.0.0` on a trusted network - there is no login yet)
- Simulated inbound email (Settings) for testing without a mailbox

## Email setup (Settings > Mailboxes)
Everything is done in the app - no files to edit. Settings > **Mailboxes** > *Add mailbox*, pick your email service (Gmail, Microsoft 365,
your own domain...) and the server details are filled in. Use **Test sending** / **Test receiving** before saving.
- Several mailboxes are supported. Tickets that arrive on a mailbox are answered from it; everything else uses the default mailbox.
- Gmail and Microsoft accounts need an *app password*, not the normal password.
- Passwords are stored in the local database on this PC and are never shown again (leave the box blank when editing to keep it).
- An old `config.json` with smtp/imap settings is converted into a mailbox automatically the first time the new version starts.

## Email notifications (Settings > Email notifications)
Tick what the helpdesk should email. Customers: ticket received, agent reply (on by default), pending, resolved, closed, reopened.
Agents: customer commented, ticket assigned to you. Each message text can be edited (placeholders such as `{id}`, `{subject}`,
`{name}`, `{link}`). Private notes are never emailed. A log of recent emails (and failures) is shown below, and on each ticket.

## Tasks, Threads, Reports, Assistant
Tasks (to-dos with owner/due date, optionally linked to a ticket), Threads (internal team discussion, optionally about a ticket),
Reports (volume, response/resolution times, by agent), a rule-based Assistant on each ticket (suggested articles/replies - no
external AI), and article suggestions on the customer portal while typing.

## Tests
`python test_app.py` - end-to-end tests against a temporary database.

## Notes / limits
- Everyone signs in with a PIN (admin / supervisor / agent roles). Customers use per-ticket links.
- SLA timers are 24/7 (no business-hours calendar). Attachments are not stored. CSAT is read-only (seeded).
- REST API under `/api/...` (tickets, agents, groups, kb, canned, automations, stats).

## Attachments, WhatsApp log, working hours
- Attach files (max 5 per message, 10 MB each) to replies and notes; customers can attach photos/files (max 3, 5 MB) on the portal
  and on their ticket page. Files arriving by email are saved on the ticket too. Programs/scripts (.exe, .bat ...) are refused.
  Files are stored in an `attachments` folder next to the database - include it in your backups.
- WhatsApp: *WhatsApp customer* lets you edit the message, opens WhatsApp and saves it on the ticket as a reply; *Log customer's
  WhatsApp reply* records what the customer wrote back.
- Settings > SLA: edit the targets, and optionally count **working hours only** (days, hours, time zone, public holidays).

## Android app
`android/` is a small native wrapper around the helpdesk web address (sign-in is remembered, attachments can be picked and
downloaded, WhatsApp/mail/phone links open the right app). It is built by GitHub Actions - no Android Studio needed:
1. On GitHub open **Actions > Build Android APK > Run workflow** (it also runs on every push that changes `android/`).
2. When it finishes, download `Helpdesk.apk` from the new entry under **Releases** (or from the run's *Artifacts*) on your phone,
   open it and allow "install unknown apps" once. Later builds install over the old one.
3. The app opens `https://desktop-obtdcvt.tail973ab1.ts.net/Helpdesk/`. To use another address long-press the app icon >
   *Change server address*.
