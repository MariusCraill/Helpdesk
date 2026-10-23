#!/usr/bin/env python3
"""Helpdesk - a local-first, Freshdesk-style ticketing system.

Pure Python standard library (no pip installs). Data lives in helpdesk.db (SQLite).
Run:  python app.py [--port 8765] [--host 127.0.0.1] [--no-browser]
"""
import argparse, base64, hashlib, hmac, imaplib, json, mimetypes, os, re, secrets, smtplib, socket, sqlite3, sys, threading, time, webbrowser
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
def _default_db():
    # Keep the database on a local disk: SQLite is unreliable on network shares (this folder may be a mapped drive).
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        d = os.path.join(os.environ["LOCALAPPDATA"], "Helpdesk")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "helpdesk.db")
    return os.path.join(BASE, "helpdesk.db")


DB_PATH = os.environ.get("HELPDESK_DB") or _default_db()
CONFIG_PATH = os.environ.get("HELPDESK_CONFIG", os.path.join(BASE, "config.json"))
STATIC = os.path.join(BASE, "static")
mimetypes.add_type("application/manifest+json", ".webmanifest")

STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
SOURCES = ["email", "portal", "phone", "whatsapp", "chat", "api"]
EDITABLE = {"subject", "status", "priority", "responder_id", "group_id", "type", "tags", "source"}
AUTH = {"disabled": False}   # tests only; the server always requires an agent PIN login
CUR = threading.local()      # the signed-in agent for the request being handled
SESSION_DAYS = 14
PUBLIC = {"/login", "/api/login", "/api/logout", "/portal", "/manifest.webmanifest", "/icon.svg", "/favicon.ico"}
SERVER = {"port": None}
BASE_PREFIX = "/Helpdesk"   # when published behind Tailscale Funnel at https://host/Helpdesk
FAILS = {}
GLOBAL_FAILS = []
TOKEN_FAILS = {}
POLL = {"last": None, "error": None, "ingested": 0, "enabled": False}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  role TEXT DEFAULT 'agent', active INTEGER DEFAULT 1, pin_hash TEXT, pin_salt TEXT, must_change INTEGER DEFAULT 0, phone TEXT);
CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, phone TEXT, company TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY, subject TEXT NOT NULL, description TEXT,
  requester_id INTEGER NOT NULL REFERENCES contacts(id),
  responder_id INTEGER REFERENCES agents(id), group_id INTEGER REFERENCES groups(id),
  status INTEGER NOT NULL DEFAULT 2, priority INTEGER NOT NULL DEFAULT 1,
  source TEXT DEFAULT 'email', type TEXT, tags TEXT DEFAULT '[]',
  due_by TEXT, fr_due_by TEXT, first_responded_at TEXT, resolved_at TEXT,
  spam INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT, access_token TEXT);
CREATE INDEX IF NOT EXISTS idx_t_status ON tickets(status, priority);
CREATE INDEX IF NOT EXISTS idx_t_resp ON tickets(responder_id);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  kind TEXT NOT NULL, private INTEGER DEFAULT 0, from_email TEXT, body TEXT,
  message_id TEXT, in_reply_to TEXT, agent_id INTEGER, contact_id INTEGER,
  delivery TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_c_ticket ON conversations(ticket_id);
CREATE TABLE IF NOT EXISTS attachments (id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, conversation_id INTEGER,
  filename TEXT, mime TEXT, size INTEGER, stored TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_att_ticket ON attachments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_c_msgid ON conversations(message_id);
CREATE TABLE IF NOT EXISTS sla_policies (priority INTEGER PRIMARY KEY, first_response_mins INTEGER, resolution_mins INTEGER);
CREATE TABLE IF NOT EXISTS canned (id INTEGER PRIMARY KEY, title TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS kb (id INTEGER PRIMARY KEY, folder TEXT, title TEXT, body TEXT, published INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS automations (id INTEGER PRIMARY KEY, name TEXT, trigger TEXT, conditions TEXT, actions TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, ticket_id INTEGER, actor TEXT, change TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS csat (id INTEGER PRIMARY KEY, ticket_id INTEGER, rating INTEGER, feedback TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS mailboxes (id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL, from_name TEXT,
  smtp_host TEXT DEFAULT '', smtp_port INTEGER, smtp_security TEXT DEFAULT 'ssl', smtp_user TEXT, smtp_password TEXT,
  imap_host TEXT DEFAULT '', imap_port INTEGER, imap_ssl INTEGER DEFAULT 1, imap_user TEXT, imap_password TEXT, imap_folder TEXT DEFAULT 'INBOX',
  poll_seconds INTEGER DEFAULT 60, active INTEGER DEFAULT 1, is_default INTEGER DEFAULT 0, last_poll TEXT, last_error TEXT, ingested INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS mail_log (id INTEGER PRIMARY KEY, ticket_id INTEGER, event TEXT, to_addr TEXT, subject TEXT, status TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_ml_ticket ON mail_log(ticket_id);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, ticket_id INTEGER, title TEXT NOT NULL, done INTEGER DEFAULT 0, assignee_id INTEGER,
  due TEXT, created_by INTEGER, created_at TEXT, done_at TEXT);
CREATE INDEX IF NOT EXISTS idx_task_ticket ON tasks(ticket_id);
CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY, title TEXT NOT NULL, ticket_id INTEGER, author_id INTEGER, closed INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS thread_msgs (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, agent_id INTEGER, body TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_tm_thread ON thread_msgs(thread_id);
"""


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- core logic
def audit(c, tid, actor, change):
    c.execute("INSERT INTO audit_log(ticket_id,actor,change,created_at) VALUES(?,?,?,?)", (tid, actor, change, iso(now())))


def get_contact(c, email, name=None, phone=None):
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("requester email is required")
    row = c.execute("SELECT id FROM contacts WHERE email=?", (email,)).fetchone()
    if row:
        if name:
            c.execute("UPDATE contacts SET name=COALESCE(NULLIF(name,''),?) WHERE id=?", (name, row["id"]))
        if phone:
            c.execute("UPDATE contacts SET phone=? WHERE id=?", (phone, row["id"]))
        return row["id"]
    cur = c.execute("INSERT INTO contacts(name,email,phone,created_at) VALUES(?,?,?,?)",
                    (name or email.split("@")[0], email, phone or None, iso(now())))
    return cur.lastrowid


DEFAULT_HOURS = {"enabled": False, "utc_offset_mins": 120,
                 "days": {"0": ["08:00", "17:00"], "1": ["08:00", "17:00"], "2": ["08:00", "17:00"], "3": ["08:00", "17:00"],
                          "4": ["08:00", "17:00"], "5": None, "6": None}, "holidays": []}


def hours_cfg(c):
    cfg = json.loads(json.dumps(DEFAULT_HOURS))
    r = c.execute("SELECT value FROM settings WHERE key='hours'").fetchone()
    if r:
        try:
            saved = json.loads(r["value"])
            for k in ("enabled", "utc_offset_mins", "days", "holidays"):
                if k in saved: cfg[k] = saved[k]
        except ValueError:
            pass
    return cfg


def _hm(s):
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60): raise ValueError("bad time")
    return h * 60 + m


def add_work_minutes(start, minutes, cfg):
    """Add `minutes` of working time to `start` (aware UTC datetime). 24/7 when business hours are off."""
    plain = start + timedelta(minutes=minutes)
    if not cfg.get("enabled"):
        return plain
    off = timedelta(minutes=int(cfg.get("utc_offset_mins") or 0))
    wins = {}
    for k, v in (cfg.get("days") or {}).items():
        try:
            if v and _hm(v[1]) > _hm(v[0]): wins[int(k)] = (_hm(v[0]), _hm(v[1]))
        except (ValueError, TypeError, IndexError):
            pass
    if not wins:
        return plain
    hol = set(cfg.get("holidays") or [])
    cur = (start + off).replace(tzinfo=None)
    remaining = float(minutes)
    for _ in range(800):
        w = wins.get(cur.weekday())
        if w and cur.strftime("%Y-%m-%d") not in hol:
            day0 = cur.replace(hour=0, minute=0, second=0, microsecond=0)
            o, cl = day0 + timedelta(minutes=w[0]), day0 + timedelta(minutes=w[1])
            if cur < o: cur = o
            if cur < cl:
                avail = (cl - cur).total_seconds() / 60
                if remaining <= avail:
                    return (cur + timedelta(minutes=remaining) - off).replace(tzinfo=timezone.utc)
                remaining -= avail
        cur = cur.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return plain


def sla_dates(c, priority, created):
    row = c.execute("SELECT * FROM sla_policies WHERE priority=?", (priority,)).fetchone()
    if not row:
        return None, None
    cfg = hours_cfg(c)
    return (iso(add_work_minutes(created, row["first_response_mins"], cfg)),
            iso(add_work_minutes(created, row["resolution_mins"], cfg)))


def api_hours_get(q, body, m):
    require_admin()
    with connect() as c:
        return 200, hours_cfg(c)


def api_hours_put(q, body, m):
    require_admin()
    days = {}
    for k in "0123456":
        v = (body.get("days") or {}).get(k)
        if v:
            try:
                a, b = _hm(v[0]), _hm(v[1])
            except (ValueError, TypeError, IndexError):
                raise ApiError(400, "times must look like 08:00")
            if b <= a: raise ApiError(400, "closing time must be after opening time")
            days[k] = ["%02d:%02d" % divmod(a, 60), "%02d:%02d" % divmod(b, 60)]
        else:
            days[k] = None
    hol = sorted({str(x).strip() for x in (body.get("holidays") or []) if str(x).strip()})
    for h in hol:
        try: datetime.strptime(h, "%Y-%m-%d")
        except ValueError: raise ApiError(400, "holiday dates must look like 2026-12-25 (%s)" % h)
    cfg = {"enabled": bool(body.get("enabled")), "utc_offset_mins": max(-720, min(840, int(body.get("utc_offset_mins", 120)))),
           "days": days, "holidays": hol}
    n = 0
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('hours',?)", (json.dumps(cfg),))
        if body.get("recalculate"):
            for t in c.execute("SELECT id,priority,created_at,first_responded_at FROM tickets WHERE deleted=0 AND status IN (2,3)").fetchall():
                fr, due = sla_dates(c, t["priority"], parse_iso(t["created_at"]))
                c.execute("UPDATE tickets SET due_by=?, fr_due_by=? WHERE id=?", (due, fr, t["id"]))
                n += 1
    return 200, {"ok": True, "recalculated": n}


def api_sla_put(q, body, m):
    require_admin()
    with connect() as c:
        for k, v in (body.get("policies") or {}).items():
            fr, rs = int(v["first_response_mins"]), int(v["resolution_mins"])
            if not (1 <= fr <= 525600 and 1 <= rs <= 525600): raise ApiError(400, "minutes must be between 1 and 525600")
            c.execute("UPDATE sla_policies SET first_response_mins=?, resolution_mins=? WHERE priority=?", (fr, rs, int(k)))
    return 200, {"ok": True}


def create_ticket(c, d, actor="system", defer_notify=False):
    subject = (d.get("subject") or "").strip()
    if not subject:
        raise ValueError("subject is required")
    priority = int(d.get("priority") or 1)
    status = int(d.get("status") or 2)
    if priority not in PRIORITY or status not in STATUS:
        raise ValueError("invalid status or priority")
    rid = get_contact(c, d.get("requester_email"), d.get("requester_name"), (d.get("requester_phone") or "").strip() or None)
    created = now()
    fr, due = sla_dates(c, priority, created)
    tags = d.get("tags") or []
    cur = c.execute(
        """INSERT INTO tickets(subject,description,requester_id,responder_id,group_id,status,priority,source,type,tags,
           due_by,fr_due_by,created_at,updated_at,access_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (subject, d.get("description") or "", rid, d.get("responder_id") or None, d.get("group_id") or None,
         status, priority, d.get("source") or "api", d.get("type"), json.dumps(tags), due, fr, iso(created), iso(created),
         secrets.token_urlsafe(18)))
    tid = cur.lastrowid
    if d.get("mailbox_id"):
        c.execute("UPDATE tickets SET mailbox_id=? WHERE id=?", (d["mailbox_id"], tid))
    add_conversation(c, tid, "incoming", d.get("description") or "(no description)", contact_id=rid,
                     from_email=(d.get("requester_email") or "").lower(), message_id=d.get("message_id"),
                     reopen=False, touch=False)
    audit(c, tid, actor, "Ticket created")
    run_automations(c, tid, "ticket_created")
    if not defer_notify:
        notify(c, tid, "created")
    return tid


def add_conversation(c, tid, kind, body, agent_id=None, contact_id=None, from_email=None, message_id=None,
                     in_reply_to=None, delivery=None, reopen=True, touch=True):
    private = 1 if kind == "note" else 0
    cur = c.execute(
        """INSERT INTO conversations(ticket_id,kind,private,from_email,body,message_id,in_reply_to,agent_id,contact_id,
           delivery,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, kind, private, from_email, body, message_id, in_reply_to, agent_id, contact_id, delivery, iso(now())))
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if kind == "reply" and not t["first_responded_at"]:
        c.execute("UPDATE tickets SET first_responded_at=? WHERE id=?", (iso(now()), tid))
    if kind == "incoming" and reopen and t["status"] in (3, 4, 5):
        c.execute("UPDATE tickets SET status=2, resolved_at=NULL WHERE id=?", (tid,))
        audit(c, tid, "customer", "Reopened by customer reply (%s -> Open)" % STATUS[t["status"]])
    if touch:
        c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (iso(now()), tid))
    return cur.lastrowid


def update_ticket(c, tid, fields, actor="system", run_rules=True):
    t = c.execute("SELECT * FROM tickets WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        raise LookupError("ticket not found")
    changes = []
    sets, vals = [], []
    for k, v in fields.items():
        if k not in EDITABLE:
            continue
        if k in ("status", "priority"):
            v = int(v)
            if v not in (STATUS if k == "status" else PRIORITY):
                raise ValueError("invalid " + k)
        elif k in ("responder_id", "group_id"):
            v = int(v) if v not in (None, "", 0, "0") else None
        elif k == "tags":
            v = json.dumps([str(x).strip() for x in (v or []) if str(x).strip()])
        if t[k] != v:
            sets.append(k + "=?")
            vals.append(v)
            changes.append("%s: %s -> %s" % (k, display(c, k, t[k]), display(c, k, v)))
    if not sets:
        return False
    now_s = iso(now())
    sets.append("updated_at=?")
    vals.append(now_s)
    new_status = dict(zip([s.split("=")[0] for s in sets], vals)).get("status")
    if new_status in (4, 5) and not t["resolved_at"]:
        sets.append("resolved_at=?")
        vals.append(now_s)
    if new_status in (2, 3):
        sets.append("resolved_at=?")
        vals.append(None)
    c.execute("UPDATE tickets SET %s WHERE id=?" % ",".join(sets), vals + [tid])
    if "priority" in fields and int(fields["priority"]) != t["priority"]:
        fr, due = sla_dates(c, int(fields["priority"]), parse_iso(t["created_at"]))
        c.execute("UPDATE tickets SET due_by=?, fr_due_by=? WHERE id=?", (due, fr, tid))
    audit(c, tid, actor, "; ".join(changes))
    if new_status and new_status != t["status"]:
        ev = {3: "pending", 4: "resolved", 5: "closed"}.get(new_status)
        if new_status == 2 and t["status"] in (3, 4, 5): ev = "reopened"
        if ev: notify(c, tid, ev)
    new_resp = dict(zip([x.split("=")[0] for x in sets], vals)).get("responder_id")
    if new_resp and new_resp != t["responder_id"] and not (getattr(CUR, "agent", None) and CUR.agent["id"] == new_resp):
        notify(c, tid, "agent_assigned", agent_id=new_resp)
    if run_rules:
        run_automations(c, tid, "ticket_updated")
    return True


def display(c, k, v):
    if v is None:
        return "-"
    if k == "status":
        return STATUS.get(v, v)
    if k == "priority":
        return PRIORITY.get(v, v)
    if k == "responder_id":
        r = c.execute("SELECT name FROM agents WHERE id=?", (v,)).fetchone()
        return r["name"] if r else v
    if k == "group_id":
        r = c.execute("SELECT name FROM groups WHERE id=?", (v,)).fetchone()
        return r["name"] if r else v
    return v


def rule_matches(t, req_email, conds):
    for cond in conds:
        f, op, val = cond.get("field"), cond.get("op", "eq"), cond.get("value")
        actual = req_email if f == "requester_email" else t[f] if f in t.keys() else None
        if f == "text":
            actual = (t["subject"] or "") + " " + (t["description"] or "")
        if actual is None:
            actual = ""
        if op in ("gte", "lte"):
            try:
                a, b = float(actual), float(val)
            except (TypeError, ValueError):
                return False
            if (op == "gte" and not a >= b) or (op == "lte" and not a <= b):
                return False
        else:
            a, b = str(actual).lower(), str(val).lower()
            if op == "eq" and a != b: return False
            if op == "neq" and a == b: return False
            if op == "contains" and b not in a: return False
            if op == "not_contains" and b in a: return False
    return True


def run_automations(c, tid, trigger):
    rules = c.execute("SELECT * FROM automations WHERE active=1 AND trigger=? ORDER BY id", (trigger,)).fetchall()
    for r in rules:
        t = c.execute("SELECT t.*, ct.email AS remail FROM tickets t JOIN contacts ct ON ct.id=t.requester_id WHERE t.id=?",
                      (tid,)).fetchone()
        try:
            conds, acts = json.loads(r["conditions"] or "[]"), json.loads(r["actions"] or "[]")
        except ValueError:
            continue
        if not rule_matches(t, t["remail"], conds):
            continue
        for a in acts:
            if "set" in a:
                update_ticket(c, tid, {a["set"]: a.get("value")}, actor="automation: " + r["name"], run_rules=False)
            elif "add_tag" in a:
                tags = json.loads(t["tags"] or "[]")
                if a["add_tag"] not in tags:
                    update_ticket(c, tid, {"tags": tags + [a["add_tag"]]}, actor="automation: " + r["name"], run_rules=False)
                    t = c.execute("SELECT t.*, ct.email AS remail FROM tickets t JOIN contacts ct ON ct.id=t.requester_id WHERE t.id=?", (tid,)).fetchone()


def ticket_dict(row, now_dt=None):
    now_dt = now_dt or now()
    d = {k: row[k] for k in row.keys()}
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["status_name"] = STATUS.get(d["status"])
    d["priority_name"] = PRIORITY.get(d["priority"])
    active = d["status"] in (2, 3)
    due, fr = parse_iso(d.get("due_by")), parse_iso(d.get("fr_due_by"))
    d["overdue"] = bool(active and due and now_dt > due)
    d["fr_overdue"] = bool(active and not d.get("first_responded_at") and fr and now_dt > fr)
    return d


TICKET_SELECT = """SELECT t.*, c.name AS requester_name, c.email AS requester_email, c.phone AS requester_phone, c.company AS requester_company, a.name AS responder_name,
  g.name AS group_name FROM tickets t JOIN contacts c ON c.id=t.requester_id
  LEFT JOIN agents a ON a.id=t.responder_id LEFT JOIN groups g ON g.id=t.group_id"""


# ---------------------------------------------------------------- email
def html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\n{3,}", "\n\n", h.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")).strip()


def strip_quoted(text):
    out = []
    for line in text.splitlines():
        if re.match(r"^\s*On .{5,120} wrote:\s*$", line) or re.match(r"^-{2,}\s*Original Message", line, re.I):
            break
        if line.startswith(">"):
            continue
        out.append(line)
    res = "\n".join(out).strip()
    return res or text.strip()


ATT_MAX = 10 * 1024 * 1024
BLOCKED_EXT = {".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".js", ".jse", ".vbs", ".vbe", ".ps1", ".jar", ".lnk", ".hta", ".dll", ".reg", ".apk"}
INLINE_OK = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


def att_dir():
    return os.path.join(os.path.dirname(DB_PATH), "attachments")


def safe_name(n):
    n = os.path.basename(str(n or "").replace("\\", "/"))
    n = re.sub(r'[\x00-\x1f<>:"|?*]', "_", n).strip(" .")
    return (n or "file")[:120]


def check_upload(name, size, max_bytes=ATT_MAX):
    if size <= 0: raise ApiError(400, "'%s' is empty" % name)
    if size > max_bytes: raise ApiError(400, "'%s' is too big (max %d MB per file)" % (name, max_bytes // 1048576))
    if os.path.splitext(name)[1].lower() in BLOCKED_EXT:
        raise ApiError(400, "'%s' is not allowed - programs and scripts can't be attached. Put it in a zip file instead." % name)


def decode_uploads(items, max_files=5, max_bytes=ATT_MAX):
    """[{name,type,data(base64)}] -> [(name, mime, bytes)], validated."""
    out = []
    if not items: return out
    if not isinstance(items, list) or len(items) > max_files:
        raise ApiError(400, "attach at most %d files at a time" % max_files)
    for it in items:
        try:
            data = base64.b64decode(str(it.get("data") or ""))
        except Exception:  # noqa
            raise ApiError(400, "could not read an attachment")
        name = safe_name(it.get("name"))
        check_upload(name, len(data), max_bytes)
        out.append((name, str(it.get("type") or mimetypes.guess_type(name)[0] or "application/octet-stream")[:100], data))
    return out


def save_attachments(c, tid, cid, files):
    for name, mime, data in files:
        aid = c.execute("INSERT INTO attachments(ticket_id,conversation_id,filename,mime,size,stored,created_at) VALUES(?,?,?,?,?,'',?)",
                        (tid, cid, name, mime, len(data), iso(now()))).lastrowid
        os.makedirs(os.path.join(att_dir(), str(tid)), exist_ok=True)
        with open(os.path.join(att_dir(), str(tid), str(aid)), "wb") as f:
            f.write(data)
        c.execute("UPDATE attachments SET stored=? WHERE id=?", ("%d/%d" % (tid, aid), aid))


def atts_by_conv(c, tid, public_only=False):
    out = {}
    sql = """SELECT a.id,a.conversation_id,a.filename,a.mime,a.size FROM attachments a
             LEFT JOIN conversations cv ON cv.id=a.conversation_id WHERE a.ticket_id=? %s ORDER BY a.id""" % (
        "AND COALESCE(cv.private,0)=0" if public_only else "")
    for r in c.execute(sql, (tid,)):
        out.setdefault(r["conversation_id"], []).append({"id": r["id"], "filename": r["filename"], "mime": r["mime"], "size": r["size"]})
    return out


def serve_attachment(row):
    path = os.path.join(att_dir(), row["stored"] or "x")
    if not row["stored"] or not os.path.isfile(path): raise ApiError(404, "file is missing")
    with open(path, "rb") as f:
        data = f.read()
    inline = row["mime"] in INLINE_OK and "HelpdeskApp" not in (getattr(CUR, "ua", "") or "")   # the Android app always downloads
    return 200, data, {"Content-Type": row["mime"] if inline else "application/octet-stream", "X-Content-Type-Options": "nosniff",
                       "Content-Security-Policy": "sandbox",
                       "Content-Disposition": "%s; filename*=UTF-8''%s" % ("inline" if inline else "attachment", quote(row["filename"]))}


def api_attachment_get(q, body, m):
    with connect() as c:
        row = c.execute("SELECT * FROM attachments WHERE id=?", (int(m.group(1)),)).fetchone()
    if not row: raise ApiError(404, "not found")
    return serve_attachment(row)


def api_portal_file(q, body, m):
    with connect() as c:
        r = customer_ticket(c, m.group(1))
        row = c.execute("""SELECT a.* FROM attachments a LEFT JOIN conversations cv ON cv.id=a.conversation_id
                           WHERE a.id=? AND a.ticket_id=? AND COALESCE(cv.private,0)=0""", (int(m.group(2)), r["id"])).fetchone()
    if not row: raise ApiError(404, "not found")
    return serve_attachment(row)


def api_whatsapp_log(q, body, m):
    """Record that the agent sent a WhatsApp (to='customer'), or what the customer said on WhatsApp (to='customer_in')."""
    tid = int(m.group(1))
    to, text = body.get("to"), str(body.get("text") or "").strip()
    if to not in ("customer", "customer_in", "agent", "share"): raise ApiError(400, "bad target")
    if not text: raise ApiError(400, "write the message first")
    ag = CUR.agent
    with connect() as c:
        t = c.execute(TICKET_SELECT + " WHERE t.id=? AND t.deleted=0", (tid,)).fetchone()
        if not t: raise ApiError(404, "ticket not found")
        who = ag["name"] if ag else "agent"
        if to == "customer":
            add_conversation(c, tid, "reply", text, agent_id=ag["id"] if ag else None, from_email=ag["email"] if ag else None,
                             delivery="sent by WhatsApp (opened by %s)" % who)
            audit(c, tid, who, "WhatsApp message to customer")
        elif to == "customer_in":
            add_conversation(c, tid, "incoming", text, contact_id=t["requester_id"], from_email=(t["requester_email"] or "").lower(),
                             delivery="received by WhatsApp (logged by %s)" % who)
            audit(c, tid, who, "Customer's WhatsApp message logged")
            notify(c, tid, "customer_comment", last_message=text)
        else:
            audit(c, tid, who, "Ticket sent by WhatsApp to %s" % (str(body.get("name") or "").strip()[:60] or ("the assigned agent" if to == "agent" else "a contact")))
    return 201, {"ok": True}


def ingest_email(c, raw, mailbox_id=None):
    msg = message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    if not addr:
        raise ValueError("email has no From address")
    subject = re.sub(r"\s+", " ", str(msg.get("Subject", "(no subject)"))).strip() or "(no subject)"
    mid = str(msg.get("Message-ID", "")).strip() or None
    irt = str(msg.get("In-Reply-To", "")).strip() or None
    refs = str(msg.get("References", "")).split()
    if mid and c.execute("SELECT 1 FROM conversations WHERE message_id=?", (mid,)).fetchone():
        return None  # duplicate
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = html_to_text(body)
    files, skipped = [], []
    for p in msg.iter_attachments():
        fn = p.get_filename()
        if not fn: continue
        fn = safe_name(fn)
        try:
            data = p.get_payload(decode=True)
            if not isinstance(data, (bytes, bytearray)): raise ValueError("not a plain file")
            check_upload(fn, len(data), 15 * 1024 * 1024)
            files.append((fn, p.get_content_type(), bytes(data)))
        except Exception:  # noqa
            skipped.append(fn)
    body = strip_quoted(body)
    if skipped:
        body += "\n\n[Attachments not stored (too big, empty or not allowed): %s]" % ", ".join(skipped)
    tid = None
    for ref in ([irt] if irt else []) + refs:
        r = c.execute("SELECT ticket_id FROM conversations WHERE message_id=?", (ref,)).fetchone()
        if r:
            tid = r["ticket_id"]
            break
    if not tid:
        m = re.search(r"\[#(\d+)\]", subject)
        if m and c.execute("SELECT 1 FROM tickets WHERE id=? AND deleted=0", (int(m.group(1)),)).fetchone():
            tid = int(m.group(1))
    if tid:
        cid = get_contact(c, addr, name)
        conv = add_conversation(c, tid, "incoming", body, contact_id=cid, from_email=addr.lower(), message_id=mid, in_reply_to=irt)
        save_attachments(c, tid, conv, files)
        audit(c, tid, addr, "Email reply received")
        run_automations(c, tid, "ticket_updated")
        notify(c, tid, "customer_comment", last_message=body)
        return tid
    new_tid = create_ticket(c, {"subject": subject, "description": body, "requester_email": addr, "requester_name": name,
                                "source": "email", "message_id": mid, "mailbox_id": mailbox_id}, actor=addr, defer_notify=bool(files))
    if files:
        first = c.execute("SELECT MIN(id) AS i FROM conversations WHERE ticket_id=?", (new_tid,)).fetchone()["i"]
        save_attachments(c, new_tid, first, files)
        notify(c, new_tid, "created")
    return new_tid


# ---------------------------------------------------------------- seed
def init_db(seed=True):
    with connect() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(agents)")}
        for col, typ in (("pin_hash", "TEXT"), ("pin_salt", "TEXT"), ("must_change", "INTEGER DEFAULT 0"), ("phone", "TEXT")):
            if col not in cols:
                c.execute("ALTER TABLE agents ADD COLUMN %s %s" % (col, typ))
        if "access_token" not in {r["name"] for r in c.execute("PRAGMA table_info(tickets)")}:
            c.execute("ALTER TABLE tickets ADD COLUMN access_token TEXT")
        if "mailbox_id" not in {r["name"] for r in c.execute("PRAGMA table_info(tickets)")}:
            c.execute("ALTER TABLE tickets ADD COLUMN mailbox_id INTEGER")
        migrate_config_mailbox(c)
        for r in c.execute("SELECT id FROM tickets WHERE access_token IS NULL").fetchall():
            c.execute("UPDATE tickets SET access_token=? WHERE id=?", (secrets.token_urlsafe(18), r["id"]))
        if seed and not c.execute("SELECT 1 FROM agents").fetchone():
            do_seed(c)


def migrate_config_mailbox(c):
    """Older installs kept one mailbox in config.json - turn it into a mailbox row the first time."""
    if c.execute("SELECT 1 FROM mailboxes").fetchone():
        return
    cfg = load_config()
    sm, im = cfg.get("smtp") or {}, cfg.get("imap") or {}
    if not (sm.get("host") or im.get("host")):
        return
    addr = (sm.get("from") or sm.get("user") or im.get("user") or "").lower()
    sec = "ssl" if sm.get("ssl", True) else ("starttls" if sm.get("starttls", True) else "none")
    c.execute("""INSERT INTO mailboxes(name,address,smtp_host,smtp_port,smtp_security,smtp_user,smtp_password,imap_host,imap_port,imap_ssl,
                 imap_user,imap_password,imap_folder,poll_seconds,active,is_default) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)""",
              ("Support mailbox", addr or "support@localhost", sm.get("host") or "", sm.get("port"), sec, sm.get("user"), sm.get("password"),
               im.get("host") or "", im.get("port"), 1 if im.get("ssl", True) else 0, im.get("user"), im.get("password"),
               im.get("folder", "INBOX"), int(im.get("poll_seconds", 60))))


def do_seed(c):
    c.executemany("INSERT INTO agents(name,email,role) VALUES(?,?,?)",
                  [("Marius", "marius@example.local", "admin"), ("Sam Support", "sam@example.local", "agent"),
                   ("Dana Billing", "dana@example.local", "agent")])
    c.executemany("INSERT INTO groups(name) VALUES(?)", [("Support",), ("Billing",), ("Technical",)])
    c.executemany("INSERT INTO sla_policies VALUES(?,?,?)", [(1, 1440, 4320), (2, 480, 2880), (3, 120, 1440), (4, 30, 240)])
    c.executemany("INSERT INTO canned(title,body) VALUES(?,?)", [
        ("Greeting", "Hi,\n\nThanks for getting in touch. "),
        ("Need more info", "Could you send us a screenshot and the exact steps you took so we can reproduce this?"),
        ("Resolved - closing", "Glad we could sort this out. We'll mark this ticket as resolved - just reply if you need anything else."),
    ])
    c.executemany("INSERT INTO kb(folder,title,body,published,updated_at) VALUES(?,?,?,?,?)", [
        ("Getting started", "How do I reset my password?", "Use the 'Forgot password' link on the sign-in page. A reset link is emailed to you and is valid for 1 hour.", 1, iso(now())),
        ("Billing", "Where can I find my invoices?", "Invoices are emailed monthly. Reply to any invoice email or contact the Billing team for copies.", 1, iso(now())),
        ("Internal", "Escalation procedure (internal)", "Urgent tickets: call the on-call technician before replying.", 0, iso(now())),
    ])
    c.executemany("INSERT INTO automations(name,trigger,conditions,actions) VALUES(?,?,?,?)", [
        ("Outage keywords -> Urgent", "ticket_created", json.dumps([{"field": "text", "op": "contains", "value": "outage"}]),
         json.dumps([{"set": "priority", "value": 4}, {"add_tag": "outage"}])),
        ("Invoice mentions -> Billing group", "ticket_created", json.dumps([{"field": "text", "op": "contains", "value": "invoice"}]),
         json.dumps([{"set": "group_id", "value": 2}])),
        ("Auto-assign billing", "ticket_updated", json.dumps([{"field": "group_id", "op": "eq", "value": 2}, {"field": "responder_id", "op": "eq", "value": ""}]),
         json.dumps([{"set": "responder_id", "value": 3}])),
    ])
    demo = [
        ("Cannot log in to my account", "Hi, I keep getting 'invalid password' even after resetting. Please help.", "thandi@acme.example", "Thandi Nkosi", 2, 2, "email", 2),
        ("Invoice #4471 looks wrong", "The invoice total includes a line I did not order. Can you check this invoice?", "pieter@farmco.example", "Pieter Botha", 2, 2, "portal", None),
        ("Total outage - gate system offline", "Since 06:00 none of our gates open. This is an outage affecting 40 staff.", "lerato@estate.example", "Lerato Dlamini", 2, 4, "phone", None),
        ("Feature request: export to CSV", "It would be great to export the visitor log to CSV.", "john@estate.example", "John Smith", 2, 1, "email", 3),
    ]
    for subj, desc, em, nm, st, pr, src, resp in demo:
        tid = create_ticket(c, dict(subject=subj, description=desc, requester_email=em, requester_name=nm, status=st,
                                    priority=pr, source=src, responder_id=resp), actor="seed")
    # make the outage ticket old enough to show an SLA breach
    old = now() - timedelta(hours=5)
    c.execute("UPDATE tickets SET created_at=?, due_by=?, fr_due_by=? WHERE id=3",
              (iso(old), iso(old + timedelta(minutes=240)), iso(old + timedelta(minutes=30))))
    add_conversation(c, 1, "reply", "Hi Thandi, I've reset your account manually. Try the temporary password we emailed.",
                     agent_id=2, from_email="sam@example.local", delivery="not sent (SMTP not configured)")
    add_conversation(c, 1, "note", "Checked auth logs - 3 failed attempts from a new device.", agent_id=2)
    update_ticket(c, 1, {"status": 3}, actor="Sam Support")
    c.execute("INSERT INTO csat(ticket_id,rating,feedback,created_at) VALUES(1,5,'Quick fix!',?)", (iso(now()),))


# ---------------------------------------------------------------- API
class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def json_loads_list(v):
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return v or []


def api_list_tickets(q, body, m):
    where, args = ["t.deleted=0"], []
    view = q.get("view", ["all"])[0]
    if view == "open": where.append("t.status IN (2,3)")
    elif view == "unassigned": where.append("t.status IN (2,3) AND t.responder_id IS NULL")
    elif view == "closed": where.append("t.status IN (4,5)")
    elif view.startswith("agent:"): where.append("t.status IN (2,3) AND t.responder_id=?"); args.append(int(view[6:]))
    for key, col in (("status", "t.status"), ("priority", "t.priority"), ("group", "t.group_id"), ("agent", "t.responder_id")):
        if q.get(key, [""])[0]:
            where.append(col + "=?"); args.append(int(q[key][0]))
    term = q.get("q", [""])[0].strip()
    if term:
        like = "%" + term + "%"
        where.append("(t.subject LIKE ? OR t.description LIKE ? OR c.email LIKE ? OR c.name LIKE ? OR CAST(t.id AS TEXT)=?)")
        args += [like, like, like, like, term.lstrip("#")]
    with connect() as c:
        rows = c.execute(TICKET_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY t.updated_at DESC LIMIT 500", args).fetchall()
    out = [ticket_dict(r) for r in rows]
    if view == "overdue":
        out = [t for t in out if t["overdue"] or t["fr_overdue"]]
    return 200, out


def api_get_ticket(q, body, m):
    with connect() as c:
        r = c.execute(TICKET_SELECT + " WHERE t.id=? AND t.deleted=0", (int(m.group(1)),)).fetchone()
        if not r: raise ApiError(404, "ticket not found")
        convs = [dict(x) for x in c.execute(
            """SELECT cv.*, a.name AS agent_name, ct.name AS contact_name FROM conversations cv
               LEFT JOIN agents a ON a.id=cv.agent_id LEFT JOIN contacts ct ON ct.id=cv.contact_id
               WHERE ticket_id=? ORDER BY cv.id""", (r["id"],))]
        log = [dict(x) for x in c.execute("SELECT * FROM audit_log WHERE ticket_id=? ORDER BY id DESC LIMIT 50", (r["id"],))]
        ab = atts_by_conv(c, r["id"])
        for cv in convs: cv["attachments"] = ab.get(cv["id"], [])
    d = ticket_dict(r)
    with connect() as c2:
        d["emails"] = [dict(x) for x in c2.execute("SELECT * FROM mail_log WHERE ticket_id=? ORDER BY id DESC LIMIT 30", (d["id"],))]
    d["conversations"], d["activity"] = convs, log
    return 200, d


def api_create_ticket(q, body, m):
    if body.get("tags") is not None: body["tags"] = json_loads_list(body["tags"])
    with connect() as c:
        tid = create_ticket(c, body, actor=body.get("actor") or "agent")
    return 201, {"id": tid}


def api_update_ticket(q, body, m):
    tid = int(m.group(1))
    if "tags" in body: body["tags"] = json_loads_list(body["tags"])
    with connect() as c:
        try:
            changed = update_ticket(c, tid, body, actor=body.get("actor") or "agent")
        except LookupError:
            raise ApiError(404, "ticket not found")
    return 200, {"changed": changed}


def api_delete_ticket(q, body, m):
    with connect() as c:
        c.execute("UPDATE tickets SET deleted=1 WHERE id=?", (int(m.group(1)),))
        audit(c, int(m.group(1)), "agent", "Ticket deleted")
    return 200, {"ok": True}


def api_reply(q, body, m):
    tid = int(m.group(1))
    kind = body.get("kind", "reply")
    text = (body.get("body") or "").strip()
    if kind not in ("reply", "note") or not text:
        raise ApiError(400, "kind must be reply|note and body is required")
    agent_id = (CUR.agent["id"] if getattr(CUR, "agent", None) else body.get("agent_id"))
    files = decode_uploads(body.get("attachments"))
    with connect() as c:
        t = c.execute(TICKET_SELECT + " WHERE t.id=? AND t.deleted=0", (tid,)).fetchone()
        if not t: raise ApiError(404, "ticket not found")
        agent = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone() if agent_id else None
        delivery, mid, last_in = None, None, None
        if kind == "reply":
            last = c.execute("SELECT message_id FROM conversations WHERE ticket_id=? AND message_id IS NOT NULL ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            last_in = last["message_id"] if last else None
            subj = t["subject"] if "[#%d]" % tid in t["subject"] else "Re: %s [#%d]" % (t["subject"], tid)
            if next(e for e in notify_cfg(c)["events"] if e["key"] == "reply")["enabled"]:
                delivery, mid = send_email(t["requester_email"], subj, text, last_in, mailbox_id=t["mailbox_id"], attachments=files)
                log_mail(tid, "reply", t["requester_email"], subj, delivery, c)
            else:
                delivery, mid = "saved, not emailed (reply emails are switched off in Settings > Notifications)", None
        conv = add_conversation(c, tid, kind, text, agent_id=agent_id, from_email=agent["email"] if agent else None,
                                message_id=mid, in_reply_to=last_in, delivery=delivery)
        save_attachments(c, tid, conv, files)
        audit(c, tid, agent["name"] if agent else "agent", "Added %s" % ("public reply" if kind == "reply" else "private note"))
        if body.get("status"):
            update_ticket(c, tid, {"status": body["status"]}, actor=agent["name"] if agent else "agent")
        if agent and not t["responder_id"]:
            update_ticket(c, tid, {"responder_id": agent["id"]}, actor=agent["name"], run_rules=False)
    return 201, {"ok": True, "delivery": delivery}


def api_simple_list(table, order="id"):
    def f(q, body, m):
        with connect() as c:
            return 200, [dict(r) for r in c.execute("SELECT * FROM %s ORDER BY %s" % (table, order))]
    return f


ROLES = ("admin", "supervisor", "agent")
PUBLIC_COLS = "id,name,email,role,active,(pin_hash IS NOT NULL) AS has_pin,must_change,phone"


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 100000).hex()


def find_agent_by_pin(c, pin):
    for a in c.execute("SELECT * FROM agents WHERE pin_hash IS NOT NULL AND active=1"):
        if hmac.compare_digest(hash_pin(pin, a["pin_salt"]), a["pin_hash"]):
            return a
    return None


def check_new_pin(c, pin, agent_id=None):
    if not re.fullmatch(r"\d{6,12}", pin):
        raise ApiError(400, "PIN must be 6-12 digits")
    a = find_agent_by_pin(c, pin)
    if a and a["id"] != agent_id:
        raise ApiError(409, "that PIN is already used by another agent - choose a different one")


def set_pin(c, agent_id, pin, must_change=0):
    salt = secrets.token_hex(16)
    c.execute("UPDATE agents SET pin_hash=?, pin_salt=?, must_change=? WHERE id=?", (hash_pin(pin, salt), salt, int(must_change), agent_id))


def gen_pin(c):
    for _ in range(50):
        pin = "%06d" % secrets.randbelow(10 ** 6)
        if not find_agent_by_pin(c, pin):
            return pin
    raise ApiError(500, "could not generate a unique PIN")


def require_admin():
    a = getattr(CUR, "agent", None)
    if not a or a["role"] != "admin":
        raise ApiError(403, "admin only")


def api_agents_list(q, body, m):
    with connect() as c:
        return 200, [dict(r) for r in c.execute("SELECT %s FROM agents ORDER BY name" % PUBLIC_COLS)]


def api_agent_create(q, body, m):
    require_admin()
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    role = body.get("role") or "agent"
    if not name or "@" not in email:
        raise ApiError(400, "name and a valid email are required")
    if role not in ROLES:
        raise ApiError(400, "role must be one of: " + ", ".join(ROLES))
    pin = str(body.get("pin") or "").strip()
    with connect() as c:
        if pin == "auto":
            pin = gen_pin(c)
        elif pin:
            check_new_pin(c, pin)
        try:
            cur = c.execute("INSERT INTO agents(name,email,role,phone) VALUES(?,?,?,?)", (name, email, role, (str(body.get("phone") or "").strip() or None)))
        except sqlite3.IntegrityError:
            raise ApiError(409, "an agent with that email already exists")
        if pin:
            set_pin(c, cur.lastrowid, pin, must_change=1)  # temporary PIN: the agent chooses their own at first login
    return 201, {"id": cur.lastrowid, "pin": pin or None}


def other_admins(c, aid):
    return c.execute("SELECT COUNT(*) n FROM agents WHERE role='admin' AND active=1 AND id<>?", (aid,)).fetchone()["n"]


def api_agent_update(q, body, m):
    require_admin()
    aid = int(m.group(1))
    sets, vals = [], []
    if "name" in body:
        if not str(body["name"]).strip(): raise ApiError(400, "name cannot be empty")
        sets.append("name=?"); vals.append(str(body["name"]).strip())
    if "email" in body:
        e = str(body["email"]).strip().lower()
        if "@" not in e: raise ApiError(400, "invalid email")
        sets.append("email=?"); vals.append(e)
    if "phone" in body:
        sets.append("phone=?"); vals.append(str(body["phone"] or "").strip() or None)
    if "role" in body:
        if body["role"] not in ROLES: raise ApiError(400, "invalid role")
        sets.append("role=?"); vals.append(body["role"])
    new_pin = None
    with connect() as c:
        row = c.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
        if not row:
            raise ApiError(404, "agent not found")
        if "role" in body and row["role"] == "admin" and body["role"] != "admin" and not other_admins(c, aid):
            raise ApiError(400, "there must be at least one admin")
        if "pin" in body:
            pin = str(body["pin"] or "").strip()
            if pin == "auto":
                pin = gen_pin(c)
            if pin:
                check_new_pin(c, pin, aid)
                set_pin(c, aid, pin, must_change=0 if aid == CUR.agent["id"] else 1)
                new_pin = pin
            else:
                if row["role"] == "admin" and not other_admins(c, aid):
                    raise ApiError(400, "the only admin must keep a PIN")
                c.execute("UPDATE agents SET pin_hash=NULL, pin_salt=NULL WHERE id=?", (aid,))
        if sets:
            try:
                c.execute("UPDATE agents SET %s WHERE id=?" % ",".join(sets), vals + [aid])
            except sqlite3.IntegrityError:
                raise ApiError(409, "an agent with that email already exists")
    return 200, {"ok": True, "pin": new_pin}


def api_agent_delete(q, body, m):
    require_admin()
    aid = int(m.group(1))
    if CUR.agent["id"] == aid:
        raise ApiError(400, "you cannot delete your own account")
    with connect() as c:
        row = c.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
        if not row:
            raise ApiError(404, "agent not found")
        if row["role"] == "admin" and not other_admins(c, aid):
            raise ApiError(400, "cannot delete the last admin")
        n = c.execute("UPDATE tickets SET responder_id=NULL WHERE responder_id=?", (aid,)).rowcount
        c.execute("DELETE FROM agents WHERE id=?", (aid,))
    return 200, {"ok": True, "unassigned_tickets": n}


def api_me(q, body, m):
    a = CUR.agent
    return 200, {"id": a["id"], "name": a["name"], "email": a["email"], "role": a["role"], "must_change": bool(a["must_change"]),
                 "public_url": (load_config().get("public_url") or "").rstrip("/")}


def api_me_pin(q, body, m):
    a = CUR.agent
    pin = str(body.get("pin") or "").strip()
    with connect() as c:
        row = c.execute("SELECT * FROM agents WHERE id=?", (a["id"],)).fetchone()
        if not row["must_change"]:
            if not hmac.compare_digest(hash_pin(str(body.get("current") or ""), row["pin_salt"]), row["pin_hash"]):
                raise ApiError(403, "current PIN is wrong")
        if hmac.compare_digest(hash_pin(pin, row["pin_salt"]), row["pin_hash"]):
            raise ApiError(400, "choose a different PIN from the current one")
        check_new_pin(c, pin, a["id"])
        set_pin(c, a["id"], pin, 0)
        row = c.execute("SELECT * FROM agents WHERE id=?", (a["id"],)).fetchone()
    return 200, {"ok": True}, {"Set-Cookie": "hd_sess=%s; Max-Age=%d%s" % (make_session(row), SESSION_DAYS * 86400, CUR.cookie_flags)}


def token_ok(ip):
    t = time.time()
    TOKEN_FAILS[ip] = [x for x in TOKEN_FAILS.get(ip, []) if t - x < 300]
    return len(TOKEN_FAILS[ip]) < 20


def customer_ticket(c, token):
    ip = getattr(CUR, "ip", "?")
    if not token_ok(ip):
        raise ApiError(429, "too many attempts - try again later")
    r = c.execute(TICKET_SELECT + " WHERE t.access_token=? AND t.deleted=0", (token,)).fetchone() if token else None
    if not r:
        TOKEN_FAILS.setdefault(ip, []).append(time.time())
        raise ApiError(404, "ticket not found")
    return r


def api_portal_ticket_view(q, body, m):
    with connect() as c:
        r = customer_ticket(c, m.group(1))
        ab = atts_by_conv(c, r["id"], public_only=True)
        convs = c.execute("""SELECT cv.id, cv.kind, cv.body, cv.created_at, a.name AS agent_name FROM conversations cv
                             LEFT JOIN agents a ON a.id=cv.agent_id WHERE cv.ticket_id=? AND cv.private=0 ORDER BY cv.id""", (r["id"],)).fetchall()
    return 200, {"id": r["id"], "subject": r["subject"], "status": r["status"], "status_name": STATUS.get(r["status"]),
                 "created_at": r["created_at"], "updated_at": r["updated_at"], "requester_name": r["requester_name"],
                 "messages": [{"from": "you" if x["kind"] == "incoming" else (x["agent_name"] or "Support"), "mine": x["kind"] == "incoming",
                               "body": x["body"], "created_at": x["created_at"], "attachments": ab.get(x["id"], [])} for x in convs]}


def api_portal_ticket_reply(q, body, m):
    text = str(body.get("body") or "").strip()
    files = decode_uploads(body.get("attachments"), 3, 5 * 1024 * 1024)
    if (not text and not files) or len(text) > 5000:
        raise ApiError(400, "write a message (max 5000 characters)")
    text = text or "(attachment)"
    with connect() as c:
        r = customer_ticket(c, m.group(1))
        conv = add_conversation(c, r["id"], "incoming", text, contact_id=r["requester_id"], from_email=(r["requester_email"] or "").lower(), reopen=True)
        save_attachments(c, r["id"], conv, files)
        audit(c, r["id"], "customer", "Customer replied from their ticket link")
        notify(c, r["id"], "customer_comment", last_message=text)
    return 201, {"ok": True}


def bootstrap_admin():
    """First run after upgrade: make Marius an admin and give him the access PIN from access_pin.txt."""
    with connect() as c:
        if c.execute("SELECT 1 FROM agents WHERE role='admin' AND pin_hash IS NOT NULL").fetchone():
            return
        pin = load_pin()
        a = c.execute("SELECT * FROM agents WHERE lower(name)='marius'").fetchone()
        if a:
            c.execute("UPDATE agents SET role='admin', active=1 WHERE id=?", (a["id"],))
            aid = a["id"]
        else:
            aid = c.execute("INSERT INTO agents(name,email,role) VALUES('Marius','mariuscraill@gmail.com','admin')").lastrowid
        if not re.fullmatch(r"\d{6,12}", pin):
            pin = gen_pin(c)
        set_pin(c, aid, pin, 0)
        print("Admin account ready: Marius (PIN set from access_pin.txt)")


def api_contact_update(q, body, m):
    sets, vals = [], []
    for k in ("name", "phone"):
        if k in body:
            sets.append(k + "=?"); vals.append((str(body[k]).strip() or None) if body[k] is not None else None)
    if not sets:
        return 200, {"ok": True}
    with connect() as c:
        cur = c.execute("UPDATE contacts SET %s WHERE id=?" % ",".join(sets), vals + [int(m.group(1))])
        if not cur.rowcount:
            raise ApiError(404, "contact not found")
    return 200, {"ok": True}


def api_stats(q, body, m):
    with connect() as c:
        rows = [ticket_dict(r) for r in c.execute("SELECT * FROM tickets WHERE deleted=0")]
        sat = c.execute("SELECT AVG(rating) a, COUNT(*) n FROM csat").fetchone()
    active = [t for t in rows if t["status"] in (2, 3)]
    frs = [(parse_iso(t["first_responded_at"]) - parse_iso(t["created_at"])).total_seconds() / 60 for t in rows if t["first_responded_at"]]
    return 200, {
        "total": len(rows), "open": sum(t["status"] == 2 for t in rows), "pending": sum(t["status"] == 3 for t in rows),
        "resolved": sum(t["status"] == 4 for t in rows), "closed": sum(t["status"] == 5 for t in rows),
        "unassigned": sum(t["responder_id"] is None for t in active),
        "overdue": sum(t["overdue"] for t in rows), "fr_overdue": sum(t["fr_overdue"] for t in rows),
        "avg_first_response_mins": round(sum(frs) / len(frs), 1) if frs else None,
        "by_priority": {PRIORITY[p]: sum(t["priority"] == p for t in active) for p in PRIORITY},
        "csat_avg": round(sat["a"], 2) if sat["n"] else None, "csat_count": sat["n"],
    }


def api_crud(table, cols, extra=None):
    def create(q, body, m):
        vals = [body.get(k) for k in cols]
        with connect() as c:
            cur = c.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ",".join("?" * len(cols))), vals)
            if table == "kb": c.execute("UPDATE kb SET updated_at=? WHERE id=?", (iso(now()), cur.lastrowid))
        return 201, {"id": cur.lastrowid}
    def update(q, body, m):
        ks = [k for k in cols + ["active", "published"] if k in body]
        if not ks: return 200, {"ok": True}
        with connect() as c:
            c.execute("UPDATE %s SET %s WHERE id=?" % (table, ",".join(k + "=?" for k in ks)), [body[k] for k in ks] + [int(m.group(1))])
        return 200, {"ok": True}
    def delete(q, body, m):
        with connect() as c:
            c.execute("DELETE FROM %s WHERE id=?" % table, (int(m.group(1)),))
        return 200, {"ok": True}
    return create, update, delete


def api_automation_create(q, body, m):
    for k in ("conditions", "actions"):
        v = body.get(k)
        if isinstance(v, str):
            try: v = json.loads(v)
            except ValueError: raise ApiError(400, k + " must be valid JSON")
        if not isinstance(v, list): raise ApiError(400, k + " must be a JSON list")
        body[k] = json.dumps(v)
    if not body.get("name") or body.get("trigger") not in ("ticket_created", "ticket_updated"):
        raise ApiError(400, "name and trigger (ticket_created|ticket_updated) required")
    with connect() as c:
        cur = c.execute("INSERT INTO automations(name,trigger,conditions,actions) VALUES(?,?,?,?)",
                        (body["name"], body["trigger"], body["conditions"], body["actions"]))
    return 201, {"id": cur.lastrowid}


def api_simulate_inbound(q, body, m):
    msg = EmailMessage()
    msg["From"] = "%s <%s>" % (body.get("name") or "", body.get("from") or "")
    msg["To"] = "support@helpdesk.local"
    msg["Subject"] = body.get("subject") or "(no subject)"
    msg["Message-ID"] = make_msgid(domain="sim.local")
    if body.get("in_reply_to"): msg["In-Reply-To"] = body["in_reply_to"]
    msg.set_content(body.get("body") or "")
    with connect() as c:
        tid = ingest_email(c, msg.as_bytes())
    return 201, {"ticket_id": tid}


def api_poll(q, body, m):
    n = poll_imap_once()
    return 200, {"ingested": n, **POLL}


def session_secret():
    path = os.path.join(os.path.dirname(DB_PATH), "session_secret.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            return v.encode()
    except OSError:
        pass
    v = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(v)
    return v.encode()


_SECRET = {}


def sess_sig(aid, exp, pin_hash):
    if "k" not in _SECRET:
        _SECRET["k"] = session_secret()
    return hmac.new(_SECRET["k"], ("%s.%s.%s" % (aid, exp, pin_hash)).encode(), hashlib.sha256).hexdigest()


def make_session(agent):
    exp = int(time.time()) + SESSION_DAYS * 86400
    return "%s.%s.%s" % (agent["id"], exp, sess_sig(agent["id"], exp, agent["pin_hash"]))


def agent_from_cookie(cookie_header):
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k != "hd_sess":
            continue
        try:
            aid, exp, sig = v.split(".")
            if int(exp) < time.time():
                return None
            with connect() as c:
                a = c.execute("SELECT * FROM agents WHERE id=? AND active=1 AND pin_hash IS NOT NULL", (int(aid),)).fetchone()
            if a and hmac.compare_digest(sig, sess_sig(a["id"], exp, a["pin_hash"])):
                return a
        except (ValueError, TypeError):
            return None
    return None


def lan_ips():
    ips = set()
    try:
        so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        so.connect(("10.255.255.255", 1))
        ips.add(so.getsockname()[0])
        so.close()
    except OSError:
        pass
    try:
        for i in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not i.startswith("127."):
                ips.add(i)
    except OSError:
        pass
    return sorted(ips)


def load_pin():
    pin = os.environ.get("HELPDESK_PIN")
    if pin:
        return pin
    path = os.path.join(os.path.dirname(DB_PATH), "access_pin.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            pin = f.read().strip()
    except OSError:
        pin = ""
    if not pin:
        pin = "%06d" % secrets.randbelow(10 ** 6)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(pin)
    return pin


def api_config(q, body, m):
    cfg = load_config()
    require_admin()
    urls = ["http://%s:%s/" % (ip, SERVER["port"]) for ip in lan_ips()]
    with connect() as c:
        imap_ok = bool(c.execute("SELECT 1 FROM mailboxes WHERE active=1 AND imap_host<>''").fetchone())
        smtp_ok = bool(c.execute("SELECT 1 FROM mailboxes WHERE active=1 AND smtp_host<>''").fetchone())
    return 200, {"lan_urls": urls, "pin_required": True, "imap_configured": imap_ok,
                 "smtp_configured": smtp_ok, "poll": POLL, "db": DB_PATH}


def api_portal_articles(q, body, m):
    with connect() as c:
        return 200, [dict(r) for r in c.execute("SELECT id,folder,title,body FROM kb WHERE published=1 ORDER BY folder,title")]


def api_portal_ticket(q, body, m):
    body = dict(body, source="portal", status=2, priority=2, responder_id=None, group_id=None)
    body["description"] = (body.get("description") or "").strip()
    files = decode_uploads(body.pop("attachments", None), 3, 5 * 1024 * 1024)
    with connect() as c:
        tid = create_ticket(c, body, actor=body.get("requester_email") or "portal")
        if files:
            save_attachments(c, tid, c.execute("SELECT MIN(id) AS i FROM conversations WHERE ticket_id=?", (tid,)).fetchone()["i"], files)
        tok = c.execute("SELECT access_token FROM tickets WHERE id=?", (tid,)).fetchone()["access_token"]
    return 201, {"id": tid, "path": "/t/" + tok}


# ---------------------------------------------------------------- mailboxes
MB_FIELDS = ("name", "address", "from_name", "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password",
             "imap_host", "imap_port", "imap_ssl", "imap_user", "imap_password", "imap_folder", "poll_seconds", "active")
MB_SECRET = ("smtp_password", "imap_password")
POLL_LAST = {}   # mailbox id -> monotonic time of the last poll


def mailbox_public(r):
    d = {k: r[k] for k in r.keys() if k not in MB_SECRET}
    d["has_smtp_password"] = bool(r["smtp_password"])
    d["has_imap_password"] = bool(r["imap_password"])
    d["can_send"] = bool(r["smtp_host"])
    d["can_receive"] = bool(r["imap_host"])
    return d


def pick_mailbox(c, mailbox_id=None, need="smtp_host"):
    """The mailbox to use: the ticket's own, else the default, else any active one that can do the job."""
    if mailbox_id:
        r = c.execute("SELECT * FROM mailboxes WHERE id=? AND active=1 AND %s<>''" % need, (mailbox_id,)).fetchone()
        if r: return r
    return (c.execute("SELECT * FROM mailboxes WHERE active=1 AND %s<>'' ORDER BY is_default DESC, id LIMIT 1" % need).fetchone())


def smtp_connect(mb):
    host, port = mb["smtp_host"], int(mb["smtp_port"] or 0)
    sec = mb["smtp_security"] or "ssl"
    if sec == "ssl":
        s = smtplib.SMTP_SSL(host, port or 465, timeout=20)
    else:
        s = smtplib.SMTP(host, port or 587, timeout=20)
        s.ehlo()
        if sec == "starttls":
            s.starttls(); s.ehlo()
    if mb["smtp_user"]:
        s.login(mb["smtp_user"], mb["smtp_password"] or "")
    return s


def imap_connect(mb):
    host, port = mb["imap_host"], int(mb["imap_port"] or 0)
    M = imaplib.IMAP4_SSL(host, port or 993, timeout=20) if mb["imap_ssl"] else imaplib.IMAP4(host, port or 143, timeout=20)
    M.login(mb["imap_user"] or mb["address"], mb["imap_password"] or "")
    return M


def friendly_mail_error(e, mb, what):
    s = str(e)
    low = s.lower()
    host = mb["smtp_host"] if what == "smtp" else mb["imap_host"]
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in low:
        return "Could not reach %s - check the server name and port, and that this PC has internet." % host
    if isinstance(e, socket.gaierror) or "name or service not known" in low or "getaddrinfo" in low:
        return "The server name '%s' was not found - check the spelling." % host
    if "authentication" in low or "auth" in low and "fail" in low or "invalid credentials" in low or "login" in low and "fail" in low or "535" in s:
        hint = " Gmail and Microsoft 365 need an app password, not your normal password." if any(x in (host or "").lower() for x in ("gmail", "google", "office365", "outlook", "microsoft")) else ""
        return "The server refused the user name or password." + hint
    if "wrong version number" in low or "ssl" in low:
        return "Security setting looks wrong for this port - try the other option (SSL/TLS vs STARTTLS)."
    if "refused" in low:
        return "The server at %s refused the connection - check the port." % host
    return s


def test_mailbox(mb, which):
    try:
        if which == "smtp":
            if not mb["smtp_host"]: return False, "No outgoing (SMTP) server entered."
            s = smtp_connect(mb); s.quit()
            return True, "Outgoing mail works - connected and signed in to %s." % mb["smtp_host"]
        if not mb["imap_host"]: return False, "No incoming (IMAP) server entered."
        M = imap_connect(mb)
        typ, _ = M.select(mb["imap_folder"] or "INBOX", readonly=True)
        if typ != "OK":
            M.logout(); return False, "Signed in, but the folder '%s' was not found." % (mb["imap_folder"] or "INBOX")
        typ, data = M.search(None, "UNSEEN")
        n = len(data[0].split()) if data and data[0] else 0
        M.logout()
        return True, "Incoming mail works - signed in to %s (%d unread message%s waiting)." % (mb["imap_host"], n, "" if n == 1 else "s")
    except Exception as e:  # noqa
        return False, friendly_mail_error(e, mb, which)


def _mb_clean(body, existing=None):
    d = {}
    for k in MB_FIELDS:
        if k in body:
            v = body[k]
            if k in ("smtp_port", "imap_port", "poll_seconds"):
                try: v = int(v) if str(v).strip() != "" else None
                except ValueError: raise ApiError(400, "%s must be a number" % k.replace("_", " "))
            elif k in ("imap_ssl", "active"):
                v = 1 if v in (True, 1, "1", "true", "on") else 0
            else:
                v = str(v if v is not None else "").strip()
            d[k] = v
    if "address" in d:
        d["address"] = d["address"].lower()
        if "@" not in d["address"]: raise ApiError(400, "enter the mailbox email address")
    if "smtp_security" in d and d["smtp_security"] not in ("ssl", "starttls", "none"):
        raise ApiError(400, "security must be ssl, starttls or none")
    if "poll_seconds" in d and d["poll_seconds"] is not None:
        d["poll_seconds"] = max(30, d["poll_seconds"])
    for k in MB_SECRET:  # blank password on edit = keep the stored one
        if k in d and d[k] == "" and existing is not None:
            d.pop(k)
    return d


def api_mailboxes_list(q, body, m):
    require_admin()
    with connect() as c:
        return 200, [mailbox_public(r) for r in c.execute("SELECT * FROM mailboxes ORDER BY is_default DESC, name")]


def api_mailbox_create(q, body, m):
    require_admin()
    d = _mb_clean(body)
    if not d.get("name") or not d.get("address"):
        raise ApiError(400, "a name and the mailbox email address are required")
    d.setdefault("smtp_security", "ssl"); d.setdefault("imap_ssl", 1); d.setdefault("active", 1)
    d.setdefault("imap_folder", "INBOX"); d.setdefault("poll_seconds", 60)
    with connect() as c:
        first = not c.execute("SELECT 1 FROM mailboxes").fetchone()
        cols = list(d)
        cur = c.execute("INSERT INTO mailboxes(%s,is_default) VALUES(%s,?)" % (",".join(cols), ",".join("?" * len(cols))),
                        [d[k] for k in cols] + [1 if first else 0])
    return 201, {"id": cur.lastrowid}


def api_mailbox_update(q, body, m):
    require_admin()
    mid = int(m.group(1))
    with connect() as c:
        row = c.execute("SELECT * FROM mailboxes WHERE id=?", (mid,)).fetchone()
        if not row: raise ApiError(404, "mailbox not found")
        d = _mb_clean(body, existing=row)
        if "name" in d and not d["name"]: raise ApiError(400, "name cannot be empty")
        if d:
            c.execute("UPDATE mailboxes SET %s WHERE id=?" % ",".join(k + "=?" for k in d), list(d.values()) + [mid])
        if body.get("is_default"):
            c.execute("UPDATE mailboxes SET is_default=(id=?)", (mid,))
    POLL_LAST.pop(mid, None)
    return 200, {"ok": True}


def api_mailbox_delete(q, body, m):
    require_admin()
    mid = int(m.group(1))
    with connect() as c:
        row = c.execute("SELECT * FROM mailboxes WHERE id=?", (mid,)).fetchone()
        if not row: raise ApiError(404, "mailbox not found")
        c.execute("UPDATE tickets SET mailbox_id=NULL WHERE mailbox_id=?", (mid,))
        c.execute("DELETE FROM mailboxes WHERE id=?", (mid,))
        if row["is_default"]:
            c.execute("UPDATE mailboxes SET is_default=1 WHERE id=(SELECT id FROM mailboxes ORDER BY id LIMIT 1)")
    return 200, {"ok": True}


def api_mailbox_test(q, body, m):
    """Test the settings typed into the form (saved or not). A blank password falls back to the stored one."""
    require_admin()
    which = body.get("which")
    if which not in ("smtp", "imap"): raise ApiError(400, "which must be smtp or imap")
    d = _mb_clean(body)
    base = {k: None for k in MB_FIELDS}
    base["address"] = ""
    if body.get("id"):
        with connect() as c:
            row = c.execute("SELECT * FROM mailboxes WHERE id=?", (int(body["id"]),)).fetchone()
        if row: base.update({k: row[k] for k in MB_FIELDS})
    for k, v in d.items():
        if k in MB_SECRET and v == "" : continue
        base[k] = v
    if which == "smtp" and not base.get("smtp_user"): base["smtp_user"] = base["address"]
    if which == "imap" and not base.get("imap_user"): base["imap_user"] = base["address"]
    ok, msg = test_mailbox(base, which)
    return 200, {"ok": ok, "message": msg}


def api_mailbox_send_test(q, body, m):
    require_admin()
    to = str(body.get("to") or "").strip()
    if "@" not in to: raise ApiError(400, "enter the email address to send the test to")
    with connect() as c:
        mb = c.execute("SELECT * FROM mailboxes WHERE id=?", (int(m.group(1)),)).fetchone()
    if not mb: raise ApiError(404, "mailbox not found")
    status, _ = send_email(to, "Helpdesk test email", "This is a test email from your helpdesk. If you can read this, outgoing mail works.", mailbox_id=mb["id"], force_mailbox=True)
    return 200, {"ok": status == "sent", "message": "Test email sent to %s." % to if status == "sent" else status}


def send_email(to, subject, body, in_reply_to=None, mailbox_id=None, force_mailbox=False, attachments=None):
    """Returns (delivery_status, message_id). Uses the ticket's mailbox, else the default one."""
    with connect() as c:
        mb = c.execute("SELECT * FROM mailboxes WHERE id=? AND smtp_host<>''", (mailbox_id,)).fetchone() if (mailbox_id and force_mailbox) else pick_mailbox(c, mailbox_id)
    mid = make_msgid(domain=((mb["address"] if mb else "") or "helpdesk.local").split("@")[-1])
    if not mb:
        return "not sent (no mailbox with outgoing mail is set up - see Settings > Mailboxes)", mid
    m = EmailMessage()
    sender = mb["address"]
    m["From"] = formataddr((mb["from_name"], sender)) if mb["from_name"] else sender
    m["To"], m["Subject"], m["Message-ID"], m["Date"] = to, subject, mid, formatdate(localtime=True)
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    for fname, mt, data in attachments or []:
        mtype, _, sub = (mt or "application/octet-stream").partition("/")
        m.add_attachment(data, maintype=mtype or "application", subtype=sub or "octet-stream", filename=fname)
    try:
        s = smtp_connect(dict(mb, smtp_user=mb["smtp_user"] or sender))
        s.send_message(m)
        s.quit()
        return "sent", mid
    except Exception as e:  # noqa
        return "failed: " + friendly_mail_error(e, mb, "smtp"), mid


def poll_mailbox(mb):
    n, err = 0, None
    try:
        M = imap_connect(dict(mb, imap_user=mb["imap_user"] or mb["address"]))
        typ, _ = M.select(mb["imap_folder"] or "INBOX")
        if typ != "OK":
            raise RuntimeError("folder '%s' not found" % (mb["imap_folder"] or "INBOX"))
        typ, data = M.search(None, "UNSEEN")
        for num in (data[0].split() if data and data[0] else []):
            typ, parts = M.fetch(num, "(RFC822)")
            with connect() as c:
                if ingest_email(c, parts[0][1], mailbox_id=mb["id"]):
                    n += 1
        M.logout()
    except Exception as e:  # noqa
        err = friendly_mail_error(e, dict(mb, imap_host=mb["imap_host"], smtp_host=None), "imap")
    with connect() as c:
        c.execute("UPDATE mailboxes SET last_poll=?, last_error=?, ingested=ingested+? WHERE id=?", (iso(now()), err, n, mb["id"]))
    return n, err


def poll_imap_once(mailbox_id=None):
    with connect() as c:
        boxes = c.execute("SELECT * FROM mailboxes WHERE active=1 AND imap_host<>''" + (" AND id=%d" % int(mailbox_id) if mailbox_id else "")).fetchall()
    POLL["enabled"] = bool(boxes)
    total, errs = 0, []
    for mb in boxes:
        n, err = poll_mailbox(mb)
        total += n
        if err: errs.append("%s: %s" % (mb["name"], err))
    POLL["error"] = "; ".join(errs) or None
    POLL["last"] = iso(now())
    POLL["ingested"] += total
    return total


def poller_loop():
    while True:
        try:
            with connect() as c:
                boxes = c.execute("SELECT * FROM mailboxes WHERE active=1 AND imap_host<>''").fetchall()
            for mb in boxes:
                if time.monotonic() - POLL_LAST.get(mb["id"], -1e9) >= max(30, mb["poll_seconds"] or 60):
                    POLL_LAST[mb["id"]] = time.monotonic()
                    poll_imap_once(mb["id"])
        except Exception:  # noqa
            pass
        time.sleep(15)


# ---------------------------------------------------------------- email notifications
NOTIFY_SYNC = False   # tests set this so emails are sent inline
NOTIFY_EVENTS = [
    # key, label, who gets it, on by default, help text, subject, body
    ("created", "Ticket received", "customer", False, "When a ticket is opened (by email, the portal, or an agent), send the customer a confirmation.",
     "We received your request [#{id}]", "Hi {name},\n\nThanks for contacting us. We have logged your request as ticket #{id}: {subject}.\nWe'll get back to you as soon as we can.\n{link_line}\nKind regards,\nSupport"),
    ("reply", "Agent replies", "customer", True, "When an agent sends a reply from a ticket, email it to the customer. Switch off to only save replies without emailing.",
     "", ""),
    ("pending", "Ticket set to Pending", "customer", False, "When a ticket is marked Pending (waiting for the customer).",
     "Waiting for your reply on ticket #{id}", "Hi {name},\n\nWe're waiting for more information from you on ticket #{id}: {subject}.\nPlease reply to this email when you can.\n{link_line}\nKind regards,\nSupport"),
    ("resolved", "Ticket resolved", "customer", False, "When a ticket is marked Resolved.",
     "Your ticket #{id} has been resolved", "Hi {name},\n\nWe've marked ticket #{id} ({subject}) as resolved. If it isn't sorted, just reply and we'll reopen it.\n{link_line}\nKind regards,\nSupport"),
    ("closed", "Ticket closed", "customer", False, "When a ticket is closed.",
     "Ticket #{id} is closed", "Hi {name},\n\nTicket #{id} ({subject}) is now closed. Thank you for your patience.\n{link_line}\nKind regards,\nSupport"),
    ("reopened", "Ticket reopened", "customer", False, "When a resolved or closed ticket is opened again by an agent.",
     "Ticket #{id} has been reopened", "Hi {name},\n\nWe've reopened ticket #{id}: {subject}, and {agent} is looking at it again.\n{link_line}\nKind regards,\nSupport"),
    ("customer_comment", "Customer comment", "agent", False, "When a customer replies or comments, email the agent the ticket is assigned to (or all admins if nobody is).",
     "New reply from {name} on ticket #{id}", "{name} replied on ticket #{id}: {subject}\n\n\"{last_message}\"\n\nOpen the ticket: {agent_link}"),
    ("agent_assigned", "Ticket assigned to an agent", "agent", False, "When a ticket is assigned to an agent, email that agent.",
     "Ticket #{id} was assigned to you", "Hi {agent},\n\nTicket #{id} was assigned to you: {subject}\nCustomer: {name}   Priority: {priority}\n\nOpen the ticket: {agent_link}"),
]


def notify_cfg(c):
    r = c.execute("SELECT value FROM settings WHERE key='notifications'").fetchone()
    saved = {}
    if r:
        try: saved = json.loads(r["value"])
        except ValueError: saved = {}
    out = {"include_link": saved.get("include_link", True), "events": []}
    for key, label, to, on, help_, subj, body in NOTIFY_EVENTS:
        s = (saved.get("events") or {}).get(key) or {}
        out["events"].append({"key": key, "label": label, "to": to, "help": help_,
                              "enabled": bool(s.get("enabled", on)), "subject": s.get("subject") or subj, "body": s.get("body") or body,
                              "default_subject": subj, "default_body": body, "editable": bool(subj)})
    return out


def api_notifications_get(q, body, m):
    require_admin()
    with connect() as c:
        cfg = notify_cfg(c)
    cfg["placeholders"] = ["{id}", "{subject}", "{name}", "{agent}", "{status}", "{priority}", "{link}", "{link_line}", "{agent_link}", "{last_message}"]
    return 200, cfg


def api_notifications_put(q, body, m):
    require_admin()
    keys = {e[0] for e in NOTIFY_EVENTS}
    ev = {}
    for k, v in (body.get("events") or {}).items():
        if k not in keys: raise ApiError(400, "unknown event " + str(k))
        ev[k] = {"enabled": bool(v.get("enabled")), "subject": str(v.get("subject") or "")[:200], "body": str(v.get("body") or "")[:5000]}
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('notifications',?)",
                  (json.dumps({"include_link": bool(body.get("include_link", True)), "events": ev}),))
    return 200, {"ok": True}


def _render(tpl, ctx):
    for k, v in ctx.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


def log_mail(ticket_id, event, to, subject, status, conn=None):
    def go(c):
        c.execute("INSERT INTO mail_log(ticket_id,event,to_addr,subject,status,created_at) VALUES(?,?,?,?,?,?)",
                  (ticket_id, event, to, subject[:200], status[:300], iso(now())))
    if conn is not None:
        go(conn)
    else:
        with connect() as c:
            go(c)


def _deliver(tid, event, to, subject, body, mailbox_id, conn=None):
    status, _ = send_email(to, subject, body, mailbox_id=mailbox_id)
    try: log_mail(tid, event, to, subject, status, conn)
    except Exception: pass  # noqa


def notify(c, tid, event, agent_id=None, last_message=None):
    """Send the email for `event` if the admin has ticked it. Never raises - a mail problem must not break the ticket."""
    try:
        cfg = notify_cfg(c)
        ev = next(e for e in cfg["events"] if e["key"] == event)
        if not ev["enabled"] or not ev["subject"]:
            return
        t = c.execute(TICKET_SELECT + " WHERE t.id=? AND t.deleted=0", (tid,)).fetchone()
        if not t: return
        pub = (load_config().get("public_url") or "").rstrip("/")
        link = (pub + "/t/" + t["access_token"]) if pub else ""
        ctx = {"id": tid, "subject": t["subject"], "name": (t["requester_name"] or "there").split(" ")[0], "agent": t["responder_name"] or "our team",
               "status": STATUS[t["status"]], "priority": PRIORITY[t["priority"]], "link": link,
               "link_line": ("\nFollow this ticket here: " + link + "\n") if (link and cfg["include_link"]) else "",
               "agent_link": (pub + "/#t%d" % tid) if pub else "(open the helpdesk and look for ticket #%d)" % tid,
               "last_message": (last_message or "").strip()[:500]}
        if ev["to"] == "customer":
            recipients = [t["requester_email"]]
        else:
            ag = c.execute("SELECT * FROM agents WHERE id=? AND active=1", (agent_id or t["responder_id"],)).fetchone() if (agent_id or t["responder_id"]) else None
            recipients = [ag["email"]] if ag else [r["email"] for r in c.execute("SELECT email FROM agents WHERE role='admin' AND active=1")]
            if ag: ctx["agent"] = ag["name"]
        own = {r["address"] for r in c.execute("SELECT address FROM mailboxes")}
        for to in recipients:
            if not to or to.lower() in own or to.lower().endswith("@example.local"):
                continue  # never mail our own mailboxes (reply loops) or the demo addresses
            args = (tid, event, to, _render(ev["subject"], ctx), _render(ev["body"], ctx), t["mailbox_id"])
            if NOTIFY_SYNC: _deliver(*args, conn=c)
            else: threading.Thread(target=_deliver, args=args, daemon=True).start()
    except Exception:  # noqa
        pass


def api_maillog(q, body, m):
    require_admin()
    with connect() as c:
        return 200, [dict(r) for r in c.execute("SELECT * FROM mail_log ORDER BY id DESC LIMIT 100")]


# ---------------------------------------------------------------- tasks & threads
def _task_rows(c, where="", args=()):
    return [dict(r) for r in c.execute(
        """SELECT k.*, t.subject AS ticket_subject, a.name AS assignee_name FROM tasks k
           LEFT JOIN tickets t ON t.id=k.ticket_id LEFT JOIN agents a ON a.id=k.assignee_id %s
           ORDER BY k.done, CASE WHEN k.due IS NULL THEN 1 ELSE 0 END, k.due, k.id DESC""" % where, args)]


def api_tasks_list(q, body, m):
    where, args = [], []
    if q.get("ticket", [""])[0].isdigit():
        where.append("k.ticket_id=?"); args.append(int(q["ticket"][0]))
    scope = q.get("scope", ["all"])[0]
    if scope == "mine":
        where.append("k.assignee_id=?"); args.append(CUR.agent["id"])
    if scope in ("open", "mine"):
        where.append("k.done=0")
    elif scope == "done":
        where.append("k.done=1")
    with connect() as c:
        return 200, _task_rows(c, ("WHERE " + " AND ".join(where)) if where else "", args)


def _clean_due(v):
    v = str(v or "").strip()
    if not v:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ApiError(400, "due date must be YYYY-MM-DD")
    return v


def api_task_create(q, body, m):
    title = str(body.get("title") or "").strip()
    if not title:
        raise ApiError(400, "title is required")
    tid = body.get("ticket_id") or None
    with connect() as c:
        if tid and not c.execute("SELECT 1 FROM tickets WHERE id=? AND deleted=0", (int(tid),)).fetchone():
            raise ApiError(404, "ticket not found")
        aid = body.get("assignee_id") or CUR.agent["id"]
        cur = c.execute("INSERT INTO tasks(ticket_id,title,done,assignee_id,due,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (int(tid) if tid else None, title[:300], 0, int(aid), _clean_due(body.get("due")), CUR.agent["id"], iso(now())))
        if tid:
            audit(c, int(tid), CUR.agent["name"], "task added: " + title[:80])
    return 201, {"id": cur.lastrowid}


def api_task_update(q, body, m):
    k = int(m.group(1))
    sets, vals = [], []
    if "title" in body:
        t = str(body["title"]).strip()
        if not t: raise ApiError(400, "title cannot be empty")
        sets.append("title=?"); vals.append(t[:300])
    if "assignee_id" in body:
        sets.append("assignee_id=?"); vals.append(int(body["assignee_id"]) if body["assignee_id"] else None)
    if "due" in body:
        sets.append("due=?"); vals.append(_clean_due(body["due"]))
    if "done" in body:
        sets.append("done=?"); vals.append(1 if body["done"] else 0)
        sets.append("done_at=?"); vals.append(iso(now()) if body["done"] else None)
    with connect() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (k,)).fetchone()
        if not row: raise ApiError(404, "task not found")
        if sets:
            c.execute("UPDATE tasks SET %s WHERE id=?" % ",".join(sets), vals + [k])
        if "done" in body and row["ticket_id"]:
            audit(c, row["ticket_id"], CUR.agent["name"], ("task done: " if body["done"] else "task reopened: ") + row["title"][:80])
    return 200, {"ok": True}


def api_task_delete(q, body, m):
    with connect() as c:
        if not c.execute("DELETE FROM tasks WHERE id=?", (int(m.group(1)),)).rowcount:
            raise ApiError(404, "task not found")
    return 200, {"ok": True}


def api_threads_list(q, body, m):
    with connect() as c:
        return 200, [dict(r) for r in c.execute(
            """SELECT th.*, a.name AS author_name, t.subject AS ticket_subject,
                 (SELECT COUNT(*) FROM thread_msgs x WHERE x.thread_id=th.id) AS msgs
               FROM threads th LEFT JOIN agents a ON a.id=th.author_id LEFT JOIN tickets t ON t.id=th.ticket_id
               ORDER BY th.closed, th.updated_at DESC""")]


def api_thread_create(q, body, m):
    title = str(body.get("title") or "").strip()
    text = str(body.get("body") or "").strip()
    if not title or not text:
        raise ApiError(400, "title and a first message are required")
    tid = body.get("ticket_id") or None
    with connect() as c:
        if tid and not c.execute("SELECT 1 FROM tickets WHERE id=? AND deleted=0", (int(tid),)).fetchone():
            raise ApiError(404, "ticket not found")
        ts = iso(now())
        cur = c.execute("INSERT INTO threads(title,ticket_id,author_id,closed,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (title[:200], int(tid) if tid else None, CUR.agent["id"], 0, ts, ts))
        c.execute("INSERT INTO thread_msgs(thread_id,agent_id,body,created_at) VALUES(?,?,?,?)", (cur.lastrowid, CUR.agent["id"], text, ts))
    return 201, {"id": cur.lastrowid}


def api_thread_get(q, body, m):
    with connect() as c:
        th = c.execute("""SELECT th.*, a.name AS author_name, t.subject AS ticket_subject FROM threads th
                          LEFT JOIN agents a ON a.id=th.author_id LEFT JOIN tickets t ON t.id=th.ticket_id WHERE th.id=?""",
                       (int(m.group(1)),)).fetchone()
        if not th: raise ApiError(404, "thread not found")
        msgs = [dict(r) for r in c.execute("""SELECT x.*, a.name AS agent_name FROM thread_msgs x LEFT JOIN agents a ON a.id=x.agent_id
                                              WHERE x.thread_id=? ORDER BY x.id""", (th["id"],))]
    d = dict(th); d["messages"] = msgs
    return 200, d


def api_thread_reply(q, body, m):
    text = str(body.get("body") or "").strip()
    if not text: raise ApiError(400, "message is empty")
    with connect() as c:
        th = c.execute("SELECT * FROM threads WHERE id=?", (int(m.group(1)),)).fetchone()
        if not th: raise ApiError(404, "thread not found")
        ts = iso(now())
        c.execute("INSERT INTO thread_msgs(thread_id,agent_id,body,created_at) VALUES(?,?,?,?)", (th["id"], CUR.agent["id"], text, ts))
        c.execute("UPDATE threads SET updated_at=?, closed=0 WHERE id=?", (ts, th["id"]))
    return 201, {"ok": True}


def api_thread_update(q, body, m):
    with connect() as c:
        if not c.execute("SELECT 1 FROM threads WHERE id=?", (int(m.group(1)),)).fetchone():
            raise ApiError(404, "thread not found")
        if "closed" in body:
            c.execute("UPDATE threads SET closed=? WHERE id=?", (1 if body["closed"] else 0, int(m.group(1))))
    return 200, {"ok": True}


def api_thread_delete(q, body, m):
    with connect() as c:
        th = c.execute("SELECT * FROM threads WHERE id=?", (int(m.group(1)),)).fetchone()
        if not th: raise ApiError(404, "thread not found")
        if CUR.agent["role"] != "admin" and th["author_id"] != CUR.agent["id"]:
            raise ApiError(403, "only the author or an admin can delete a thread")
        c.execute("DELETE FROM thread_msgs WHERE thread_id=?", (th["id"],))
        c.execute("DELETE FROM threads WHERE id=?", (th["id"],))
    return 200, {"ok": True}


# ---------------------------------------------------------------- reports
def _mins(a, b):
    return (parse_iso(b) - parse_iso(a)).total_seconds() / 60


def api_reports(q, body, m):
    try:
        days = max(1, min(365, int(q.get("days", ["30"])[0])))
    except ValueError:
        days = 30
    end = now()
    start = end - timedelta(days=days)
    with connect() as c:
        tix = [ticket_dict(r) for r in c.execute(TICKET_SELECT + " WHERE t.deleted=0 AND t.spam=0")]
        agents = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM agents")}
        csat = c.execute("SELECT AVG(rating) a, COUNT(*) n FROM csat WHERE created_at>=?", (iso(start),)).fetchone()
    created = [t for t in tix if t["created_at"] >= iso(start)]
    resolved = [t for t in tix if t["resolved_at"] and t["resolved_at"] >= iso(start)]
    daily = {}
    for i in range(days):
        daily[(start + timedelta(days=i + 1)).strftime("%Y-%m-%d")] = [0, 0]
    for t in created:
        k = t["created_at"][:10]
        if k in daily: daily[k][0] += 1
    for t in resolved:
        k = t["resolved_at"][:10]
        if k in daily: daily[k][1] += 1
    fr = [_mins(t["created_at"], t["first_responded_at"]) for t in created if t["first_responded_at"]]
    rs = [_mins(t["created_at"], t["resolved_at"]) for t in resolved]
    fr_ok = [t for t in created if t["first_responded_at"] and t["fr_due_by"]]
    rs_ok = [t for t in resolved if t["due_by"]]
    pct = lambda ok, tot: round(100.0 * ok / tot) if tot else None
    by_agent = {}
    for t in tix:
        if t["status"] in (2, 3) or (t["resolved_at"] and t["resolved_at"] >= iso(start)):
            name = agents.get(t["responder_id"]) or "Unassigned"
            r = by_agent.setdefault(name, {"open": 0, "resolved": 0, "res_mins": []})
            if t["status"] in (2, 3): r["open"] += 1
            if t["resolved_at"] and t["resolved_at"] >= iso(start):
                r["resolved"] += 1; r["res_mins"].append(_mins(t["created_at"], t["resolved_at"]))
    agents_out = [{"agent": k, "open": v["open"], "resolved": v["resolved"],
                   "avg_resolution_mins": round(sum(v["res_mins"]) / len(v["res_mins"])) if v["res_mins"] else None}
                  for k, v in sorted(by_agent.items())]
    count = lambda items, key, names: {names[k]: sum(1 for t in items if t[key] == k) for k in names}
    src = {}
    for t in created: src[t["source"] or "other"] = src.get(t["source"] or "other", 0) + 1
    return 200, {
        "days": days, "created": len(created), "resolved": len(resolved),
        "backlog": sum(1 for t in tix if t["status"] in (2, 3)),
        "avg_first_response_mins": round(sum(fr) / len(fr)) if fr else None,
        "avg_resolution_mins": round(sum(rs) / len(rs)) if rs else None,
        "fr_sla_pct": pct(sum(1 for t in fr_ok if t["first_responded_at"] <= t["fr_due_by"]), len(fr_ok)),
        "res_sla_pct": pct(sum(1 for t in rs_ok if t["resolved_at"] <= t["due_by"]), len(rs_ok)),
        "csat_avg": round(csat["a"], 2) if csat["n"] else None, "csat_count": csat["n"],
        "daily": [{"date": k, "created": v[0], "resolved": v[1]} for k, v in daily.items()],
        "by_status": count(created, "status", STATUS), "by_priority": count(created, "priority", PRIORITY),
        "by_source": src, "by_agent": agents_out,
    }


# ---------------------------------------------------------------- assistant (local, rule-based: no external AI service)
STOP = set("a an and are as at be but by can could do does for from has have how i if in is it its me my no not of on or our please so that the their there this to was we were what when where which who why will with would you your hi hello thanks thank regards dear".split())


def _words(s):
    return [w for w in re.findall(r"[a-z0-9']+", (s or "").lower()) if len(w) > 2 and w not in STOP]


def _score(qwords, text):
    tw = set(_words(text))
    return sum(1 for w in set(qwords) if w in tw)


def suggest_articles(c, text, limit=3):
    qw = _words(text)
    if not qw:
        return []
    out = []
    for r in c.execute("SELECT id,folder,title,body FROM kb WHERE published=1"):
        s = _score(qw, r["title"]) * 3 + _score(qw, r["body"])
        if s >= 2:
            out.append((s, dict(r)))
    out.sort(key=lambda x: -x[0])
    return [a for _, a in out[:limit]]


def api_portal_suggest(q, body, m):
    text = q.get("q", [""])[0][:300]
    with connect() as c:
        return 200, [{"id": a["id"], "title": a["title"], "body": a["body"]} for a in suggest_articles(c, text)]


def api_assist(q, body, m):
    tid = int(m.group(1))
    with connect() as c:
        t = c.execute(TICKET_SELECT + " WHERE t.id=? AND t.deleted=0", (tid,)).fetchone()
        if not t: raise ApiError(404, "ticket not found")
        convs = [dict(x) for x in c.execute("SELECT kind,private,body,created_at FROM conversations WHERE ticket_id=? ORDER BY id", (tid,))]
        text = (t["subject"] or "") + " " + " ".join(x["body"] or "" for x in convs if x["kind"] == "incoming")
        qw = _words(text)
        arts = suggest_articles(c, text)
        canned = []
        for r in c.execute("SELECT id,title,body FROM canned"):
            s = _score(qw, r["title"]) * 2 + _score(qw, r["body"])
            if s >= 2: canned.append((s, dict(r)))
        canned = [x for _, x in sorted(canned, key=lambda z: -z[0])[:3]]
        sim = []
        for r in c.execute("""SELECT t.id,t.subject,t.status,t.description FROM tickets t WHERE t.deleted=0 AND t.id<>?""", (tid,)):
            s = _score(qw, r["subject"]) * 2 + _score(qw, r["description"])
            if s >= 3: sim.append((s, {"id": r["id"], "subject": r["subject"], "status": r["status"]}))
        sim = [x for _, x in sorted(sim, key=lambda z: -z[0])[:4]]
    low = text.lower()
    hint = None
    if any(w in low for w in ("outage", "down", "urgent", "asap", "not working at all", "emergency", "offline")):
        hint = 4
    elif any(w in low for w in ("cannot", "can't", "error", "failed", "broken")):
        hint = 3
    incoming = [x for x in convs if x["kind"] == "incoming"]
    last_in = incoming[-1]["body"] if incoming else (t["description"] or "")
    replies = sum(1 for x in convs if x["kind"] == "reply")
    summary = "%s via %s, %s priority, %s. %d customer message(s), %d reply(ies). Latest from customer: \"%s\"" % (
        t["requester_name"], t["source"], PRIORITY[t["priority"]], STATUS[t["status"]].lower(), len(incoming), replies,
        (last_in or "").strip().replace("\n", " ")[:200])
    return 200, {"articles": arts, "canned": canned, "similar": sim, "summary": summary,
                 "suggested_priority": PRIORITY[hint] if hint and hint > t["priority"] else None}


ac = api_crud("canned", ["title", "body"])
ak = api_crud("kb", ["folder", "title", "body", "published"])
aa = api_crud("automations", ["name", "trigger", "conditions", "actions"])


def admin_only(fn):
    def w(q, body, m):
        require_admin()
        return fn(q, body, m)
    return w

ROUTES = [
    ("GET", r"/api/tickets", api_list_tickets),
    ("POST", r"/api/tickets", api_create_ticket),
    ("GET", r"/api/tickets/(\d+)", api_get_ticket),
    ("PATCH", r"/api/tickets/(\d+)", api_update_ticket),
    ("DELETE", r"/api/tickets/(\d+)", api_delete_ticket),
    ("POST", r"/api/tickets/(\d+)/reply", api_reply),
    ("GET", r"/api/agents", api_agents_list),
    ("GET", r"/api/me", api_me),
    ("POST", r"/api/me/pin", api_me_pin),
    ("POST", r"/api/agents", api_agent_create),
    ("PATCH", r"/api/agents/(\d+)", api_agent_update),
    ("DELETE", r"/api/agents/(\d+)", api_agent_delete),
    ("PATCH", r"/api/contacts/(\d+)", api_contact_update),
    ("GET", r"/api/groups", api_simple_list("groups", "name")),
    ("GET", r"/api/contacts", api_simple_list("contacts", "name")),
    ("GET", r"/api/sla", api_simple_list("sla_policies", "priority")),
    ("GET", r"/api/stats", api_stats),
    ("GET", r"/api/canned", api_simple_list("canned", "title")),
    ("POST", r"/api/canned", ac[0]), ("PATCH", r"/api/canned/(\d+)", ac[1]), ("DELETE", r"/api/canned/(\d+)", ac[2]),
    ("GET", r"/api/kb", api_simple_list("kb", "folder,title")),
    ("POST", r"/api/kb", ak[0]), ("PATCH", r"/api/kb/(\d+)", ak[1]), ("DELETE", r"/api/kb/(\d+)", ak[2]),
    ("GET", r"/api/automations", api_simple_list("automations")),
    ("POST", r"/api/automations", admin_only(api_automation_create)), ("PATCH", r"/api/automations/(\d+)", admin_only(aa[1])),
    ("DELETE", r"/api/automations/(\d+)", admin_only(aa[2])),
    ("POST", r"/api/inbound/simulate", admin_only(api_simulate_inbound)),
    ("POST", r"/api/inbound/poll", admin_only(api_poll)),
    ("GET", r"/api/config", api_config),
    ("GET", r"/api/tasks", api_tasks_list), ("POST", r"/api/tasks", api_task_create),
    ("PATCH", r"/api/tasks/(\d+)", api_task_update), ("DELETE", r"/api/tasks/(\d+)", api_task_delete),
    ("GET", r"/api/threads", api_threads_list), ("POST", r"/api/threads", api_thread_create),
    ("GET", r"/api/threads/(\d+)", api_thread_get), ("POST", r"/api/threads/(\d+)/reply", api_thread_reply),
    ("PATCH", r"/api/threads/(\d+)", api_thread_update), ("DELETE", r"/api/threads/(\d+)", api_thread_delete),
    ("GET", r"/api/reports", api_reports),
    ("GET", r"/api/mailboxes", api_mailboxes_list), ("POST", r"/api/mailboxes", api_mailbox_create),
    ("POST", r"/api/mailboxes/test", api_mailbox_test),
    ("PATCH", r"/api/mailboxes/(\d+)", api_mailbox_update), ("DELETE", r"/api/mailboxes/(\d+)", api_mailbox_delete),
    ("POST", r"/api/mailboxes/(\d+)/send-test", api_mailbox_send_test),
    ("GET", r"/api/notifications", api_notifications_get), ("PUT", r"/api/notifications", api_notifications_put),
    ("GET", r"/api/maillog", api_maillog),
    ("GET", r"/api/attachments/(\d+)", api_attachment_get),
    ("POST", r"/api/tickets/(\d+)/whatsapp", api_whatsapp_log),
    ("GET", r"/api/hours", api_hours_get), ("PUT", r"/api/hours", api_hours_put), ("PUT", r"/api/sla", api_sla_put),
    ("GET", r"/api/tickets/(\d+)/assist", api_assist),
    ("GET", r"/api/portal/suggest", api_portal_suggest),
    ("GET", r"/api/portal/articles", api_portal_articles),
    ("POST", r"/api/portal/ticket", api_portal_ticket),
    ("GET", r"/api/portal/ticket/([A-Za-z0-9_-]{8,64})", api_portal_ticket_view),
    ("POST", r"/api/portal/ticket/([A-Za-z0-9_-]{8,64})/reply", api_portal_ticket_reply),
    ("GET", r"/api/portal/ticket/([A-Za-z0-9_-]{8,64})/file/(\d+)", api_portal_file),
]
ROUTES = [(mth, re.compile("^" + pat + "$"), fn) for mth, pat, fn in ROUTES]


class Handler(BaseHTTPRequestHandler):
    server_version = "Helpdesk/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("HELPDESK_LOG"):
            sys.stderr.write("%s\n" % (fmt % args))

    def _send(self, code, payload, ctype="application/json; charset=utf-8", headers=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _client_ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):  # behind Tailscale Funnel / a local proxy: use the real client address
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if xff:
                return xff
        return ip

    def _authorized(self, path):
        CUR.agent = None
        if path in PUBLIC or path.startswith("/api/portal/") or path.startswith("/t/"):
            return True
        if AUTH["disabled"]:
            with connect() as c:
                CUR.agent = c.execute("SELECT * FROM agents WHERE role='admin' ORDER BY id").fetchone()
            return True
        CUR.agent = agent_from_cookie(self.headers.get("Cookie"))
        return CUR.agent is not None

    def _cookie_flags(self):
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        base = (self.headers.get("X-Base-Path") or "")
        path = base if base.lower() in ("", BASE_PREFIX.lower()) else ""
        return "; Path=%s; HttpOnly; SameSite=Lax" % (path or "/") + secure

    def _login(self):
        ip, t = self._client_ip(), time.time()
        FAILS[ip] = [x for x in FAILS.get(ip, []) if t - x < 300]
        GLOBAL_FAILS[:] = [x for x in GLOBAL_FAILS if t - x < 600]
        if len(FAILS[ip]) >= 5 or len(GLOBAL_FAILS) >= 25:
            return self._send(429, {"error": "too many attempts - wait a few minutes"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        pin = str(body.get("pin", "")).strip()
        with connect() as c:
            a = find_agent_by_pin(c, pin) if pin else None
        if a:
            FAILS.pop(ip, None)
            return self._send(200, {"ok": True, "name": a["name"], "role": a["role"]}, headers={
                "Set-Cookie": "hd_sess=%s; Max-Age=%d%s" % (make_session(a), SESSION_DAYS * 86400, self._cookie_flags())})
        FAILS[ip].append(t)
        GLOBAL_FAILS.append(t)
        return self._send(401, {"error": "wrong PIN"})

    def _handle(self, method):
        url = urlparse(self.path)
        path = url.path
        if path.lower() == BASE_PREFIX.lower() or path.lower().startswith(BASE_PREFIX.lower() + "/"):  # works whether or not the proxy strips the prefix (any letter case)
            path = path[len(BASE_PREFIX):] or "/"
        if path.rstrip("/").lower() in ("/login", "/portal"):  # typed URLs: any letter case, optional trailing slash
            path = path.rstrip("/").lower()
        if path == "/api/login" and method == "POST":
            return self._login()
        CUR.ip = self._client_ip()
        CUR.ua = self.headers.get("User-Agent") or ""
        CUR.cookie_flags = self._cookie_flags()
        if path == "/api/logout" and method == "POST":
            return self._send(200, {"ok": True}, headers={"Set-Cookie": "hd_sess=; Max-Age=0%s" % self._cookie_flags()})
        if not self._authorized(path):
            if path.startswith("/api/"):
                return self._send(401, {"error": "login required"})
            with open(os.path.join(STATIC, "login.html"), "rb") as f:  # show the sign-in page right at this address (no redirect)
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if CUR.agent is not None and CUR.agent["must_change"] and path.startswith("/api/") and path not in ("/api/me", "/api/me/pin"):
            return self._send(403, {"error": "choose your own PIN first", "must_change": True})
        if path.startswith("/api/"):
            for mth, rx, fn in ROUTES:
                mm = rx.match(path)
                if mm and mth == method:
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        if n > 24 * 1024 * 1024:
                            self.close_connection = True
                            return self._send(413, {"error": "too much data - attach fewer or smaller files"})
                        raw = self.rfile.read(n) if n else b""
                        body = json.loads(raw.decode("utf-8")) if raw else {}
                        if not isinstance(body, dict):
                            raise ApiError(400, "JSON object expected")
                        res = fn(parse_qs(url.query), body, mm)
                        if isinstance(res[1], bytes):
                            hdrs = dict(res[2])
                            return self._send(res[0], res[1], hdrs.pop("Content-Type"), headers=hdrs)
                        return self._send(res[0], res[1], headers=res[2] if len(res) > 2 else None)
                    except ApiError as e:
                        return self._send(e.code, {"error": e.msg})
                    except (ValueError, KeyError, TypeError) as e:
                        return self._send(400, {"error": str(e)})
                    except sqlite3.IntegrityError as e:
                        return self._send(409, {"error": str(e)})
                    except Exception as e:  # noqa
                        return self._send(500, {"error": "internal error: %s" % e})
            if any(rx.match(path) for _, rx, _ in ROUTES):
                return self._send(405, {"error": "method not allowed"})
            return self._send(404, {"error": "not found"})
        if method != "GET":
            return self._send(405, {"error": "method not allowed"})
        if path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if path.startswith("/t/"):  # customer ticket page: the secret token in the URL is the access
            with open(os.path.join(STATIC, "ticket.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8", headers={"Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"})
        rel = {"/": "index.html", "/portal": "portal.html", "/login": "login.html"}.get(path, path.lstrip("/"))
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self._send(404, b"Not found", "text/plain")
        with open(full, "rb") as f:
            self._send(200, f.read(), (mimetypes.guess_type(full)[0] or "application/octet-stream") + "; charset=utf-8")

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")
    def do_PUT(self): self._handle("PUT")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    init_db()
    bootstrap_admin()
    SERVER["port"] = a.port
    threading.Thread(target=poller_loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = "http://%s:%d/" % ("localhost" if a.host in ("127.0.0.1", "0.0.0.0") else a.host, a.port)
    print("Helpdesk running at", url, "(Ctrl+C to stop)")
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("Network access ON. Others can open:", ", ".join("http://%s:%d/" % (ip, a.port) for ip in lan_ips()) or "(no LAN address found)")
    print("Sign in with your agent PIN (admin: Marius).")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
