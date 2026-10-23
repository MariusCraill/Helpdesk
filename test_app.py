"""End-to-end tests: starts the real server on a temp DB and exercises the HTTP API."""
import json, os, socket, sys, tempfile, threading, unittest, urllib.request, urllib.error

TMP = tempfile.mkdtemp()
os.environ["HELPDESK_DB"] = os.path.join(TMP, "test.db")
os.environ["HELPDESK_CONFIG"] = os.path.join(TMP, "none.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

PORT = None
BASE = None


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if e.headers.get_content_type() == "application/json" else raw)


def setUpModule():
    global PORT, BASE
    s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
    BASE = "http://127.0.0.1:%d" % PORT
    app.init_db()
    app.bootstrap_admin()
    app.AUTH["disabled"] = True  # most tests run as the admin; test_17 turns real PIN login on
    srv = app.ThreadingHTTPServer(("127.0.0.1", PORT), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


class T(unittest.TestCase):
    def test_01_seed_and_lists(self):
        code, tickets = call("GET", "/api/tickets?view=all")
        self.assertEqual(code, 200); self.assertEqual(len(tickets), 4)
        self.assertEqual(len(call("GET", "/api/agents")[1]), 3)
        self.assertEqual(len(call("GET", "/api/groups")[1]), 3)

    def test_02_static_pages(self):
        for p in ("/", "/portal"):
            code, body = call("GET", p)
            self.assertEqual(code, 200); self.assertIn(b"<html", body.lower())
        self.assertEqual(call("GET", "/../app.py")[0], 404)
        self.assertEqual(call("GET", "/nope")[0], 404)

    def test_03_create_validation_and_sla(self):
        self.assertEqual(call("POST", "/api/tickets", {"subject": "x"})[0], 400)  # no requester
        self.assertEqual(call("POST", "/api/tickets", {"requester_email": "a@b.c"})[0], 400)  # no subject
        self.assertEqual(call("POST", "/api/tickets", {"subject": "s", "requester_email": "a@b.c", "priority": 9})[0], 400)
        code, r = call("POST", "/api/tickets", {"subject": "Printer jams", "requester_email": "New@Cust.com", "description": "It jams", "priority": 3})
        self.assertEqual(code, 201)
        t = call("GET", "/api/tickets/%d" % r["id"])[1]
        self.assertEqual(t["requester_email"], "new@cust.com")  # lower-cased contact
        self.assertEqual(t["priority_name"], "High"); self.assertTrue(t["due_by"] and t["fr_due_by"])
        self.assertEqual(len(t["conversations"]), 1)
        self.assertEqual(call("GET", "/api/tickets/99999")[0], 404)

    def test_04_seeded_overdue_and_automations(self):
        t3 = call("GET", "/api/tickets/3")[1]
        self.assertEqual(t3["priority"], 4)  # 'outage' rule
        self.assertIn("outage", t3["tags"])
        self.assertTrue(t3["overdue"] and t3["fr_overdue"])  # 5h old, urgent SLA 30m/4h
        t2 = call("GET", "/api/tickets/2")[1]
        self.assertEqual(t2["group_name"], "Billing")  # 'invoice' rule
        views = call("GET", "/api/tickets?view=overdue")[1]
        self.assertIn(3, [x["id"] for x in views])

    def test_05_update_status_resolved_and_priority_recalc(self):
        _, r = call("POST", "/api/tickets", {"subject": "Slow wifi", "requester_email": "w@x.com", "priority": 1})
        tid = r["id"]
        before = call("GET", "/api/tickets/%d" % tid)[1]["due_by"]
        self.assertTrue(call("PATCH", "/api/tickets/%d" % tid, {"priority": 4})[1]["changed"])
        t = call("GET", "/api/tickets/%d" % tid)[1]
        self.assertNotEqual(t["due_by"], before)
        call("PATCH", "/api/tickets/%d" % tid, {"status": 4})
        t = call("GET", "/api/tickets/%d" % tid)[1]
        self.assertEqual(t["status"], 4); self.assertTrue(t["resolved_at"])
        self.assertFalse(call("PATCH", "/api/tickets/%d" % tid, {"status": 4})[1]["changed"])  # no-op
        self.assertEqual(call("PATCH", "/api/tickets/%d" % tid, {"status": 77})[0], 400)
        self.assertTrue(any("status" in a["change"] for a in t["activity"]))

    def test_06_reply_note_first_response(self):
        _, r = call("POST", "/api/tickets", {"subject": "Need help", "requester_email": "h@x.com"})
        tid = r["id"]
        code, _ = call("POST", "/api/tickets/%d/reply" % tid, {"kind": "note", "body": "internal", "agent_id": 2})
        self.assertEqual(code, 201)
        t = call("GET", "/api/tickets/%d" % tid)[1]
        self.assertIsNone(t["first_responded_at"])  # note is not a response
        code, out = call("POST", "/api/tickets/%d/reply" % tid, {"kind": "reply", "body": "Hello!", "agent_id": 2, "status": 3})
        self.assertEqual(code, 201); self.assertIn("not sent", out["delivery"])
        t = call("GET", "/api/tickets/%d" % tid)[1]
        self.assertTrue(t["first_responded_at"]); self.assertEqual(t["status"], 3)
        self.assertEqual(t["responder_id"], 1)  # auto-assigned to the signed-in agent (admin)
        self.assertEqual([c["kind"] for c in t["conversations"]], ["incoming", "note", "reply"])
        self.assertEqual(call("POST", "/api/tickets/%d/reply" % tid, {"kind": "reply", "body": ""})[0], 400)

    def test_07_inbound_email_threading_and_reopen(self):
        code, r = call("POST", "/api/inbound/simulate", {"from": "mail@cust.com", "name": "Mail Cust", "subject": "Broken widget", "body": "It broke."})
        tid = r["ticket_id"]
        t = call("GET", "/api/tickets/%d" % tid)[1]
        self.assertEqual(t["source"], "email"); self.assertEqual(t["requester_name"], "Mail Cust")
        call("PATCH", "/api/tickets/%d" % tid, {"status": 4})
        code, r2 = call("POST", "/api/inbound/simulate", {"from": "mail@cust.com", "subject": "Re: Broken widget [#%d]" % tid,
                                                           "body": "Still broken.\n\nOn Mon, 1 Jan 2026, Support wrote:\n> old text"})
        self.assertEqual(r2["ticket_id"], tid)  # threaded by [#id]
        t = call("GET", "/api/tickets/%d" % tid)[1]
        self.assertEqual(t["status"], 2)  # reopened
        self.assertEqual(t["conversations"][-1]["body"], "Still broken.")  # quote stripped
        self.assertEqual(len(t["conversations"]), 2)

    def test_08_message_id_dedupe_and_reply_header_threading(self):
        raw = (b"From: Dup <dup@x.com>\r\nTo: s@h\r\nSubject: Dup test\r\nMessage-ID: <abc@x>\r\n\r\nbody one\r\n")
        with app.connect() as c:
            a = app.ingest_email(c, raw)
            self.assertIsNotNone(a); self.assertIsNone(app.ingest_email(c, raw))  # duplicate ignored
            raw2 = (b"From: Dup <dup@x.com>\r\nTo: s@h\r\nSubject: totally different\r\nMessage-ID: <def@x>\r\n"
                    b"In-Reply-To: <abc@x>\r\n\r\nfollow up\r\n")
            self.assertEqual(app.ingest_email(c, raw2), a)  # threaded by In-Reply-To
            html = (b"From: H <h@x.com>\r\nSubject: Html mail\r\nMessage-ID: <h@x>\r\nContent-Type: text/html\r\n\r\n<p>Hello <b>there</b></p>")
            tid = app.ingest_email(c, html)
            body = c.execute("SELECT body FROM conversations WHERE ticket_id=?", (tid,)).fetchone()["body"]
            self.assertIn("Hello there", body); self.assertNotIn("<b>", body)

    def test_09_search_and_filters(self):
        self.assertTrue(any("gate" in t["subject"].lower() for t in call("GET", "/api/tickets?q=gate")[1]))
        self.assertEqual(len(call("GET", "/api/tickets?q=%233")[1]), 1)
        self.assertTrue(all(t["priority"] == 4 for t in call("GET", "/api/tickets?priority=4")[1]))
        self.assertTrue(all(t["responder_id"] is None for t in call("GET", "/api/tickets?view=unassigned")[1]))

    def test_10_crud_kb_canned_automation_portal(self):
        code, k = call("POST", "/api/kb", {"folder": "FAQ", "title": "Opening hours", "body": "9-5", "published": 1})
        self.assertEqual(code, 201)
        self.assertIn("Opening hours", [a["title"] for a in call("GET", "/api/portal/articles")[1]])
        call("PATCH", "/api/kb/%d" % k["id"], {"published": 0})
        self.assertNotIn("Opening hours", [a["title"] for a in call("GET", "/api/portal/articles")[1]])
        self.assertNotIn("Internal", [a["folder"] for a in call("GET", "/api/portal/articles")[1]])  # drafts hidden
        code, c = call("POST", "/api/canned", {"title": "T", "body": "B"}); self.assertEqual(code, 201)
        self.assertEqual(call("DELETE", "/api/canned/%d" % c["id"])[0], 200)
        self.assertEqual(call("POST", "/api/automations", {"name": "bad", "trigger": "nope", "conditions": [], "actions": []})[0], 400)
        self.assertEqual(call("POST", "/api/automations", {"name": "bad", "trigger": "ticket_created", "conditions": "{oops", "actions": "[]"})[0], 400)
        code, a = call("POST", "/api/automations", {"name": "VIP", "trigger": "ticket_created",
                       "conditions": [{"field": "requester_email", "op": "contains", "value": "@vip.com"}],
                       "actions": [{"set": "priority", "value": 3}, {"add_tag": "vip"}]})
        self.assertEqual(code, 201)
        _, r = call("POST", "/api/portal/ticket", {"requester_email": "boss@vip.com", "subject": "Hi", "description": "portal request"})
        t = call("GET", "/api/tickets/%d" % r["id"])[1]
        self.assertEqual(t["source"], "portal"); self.assertEqual(t["priority"], 3); self.assertIn("vip", t["tags"])

    def test_11_stats_delete_and_xss_safety(self):
        s = call("GET", "/api/stats")[1]
        self.assertGreaterEqual(s["total"], 4); self.assertEqual(s["csat_count"], 1); self.assertGreaterEqual(s["overdue"], 1)
        _, r = call("POST", "/api/tickets", {"subject": "<script>alert(1)</script>", "requester_email": "x@y.z"})
        self.assertEqual(call("GET", "/api/tickets/%d" % r["id"])[1]["subject"], "<script>alert(1)</script>")  # stored raw, UI escapes
        self.assertEqual(call("DELETE", "/api/tickets/%d" % r["id"])[0], 200)
        self.assertEqual(call("GET", "/api/tickets/%d" % r["id"])[0], 404)
        self.assertEqual(call("GET", "/api/config")[1]["smtp_configured"], False)

    def test_12_bad_requests(self):
        self.assertEqual(call("GET", "/api/nothing")[0], 404)
        self.assertEqual(call("PUT", "/api/tickets")[0], 405)
        req = urllib.request.Request(BASE + "/api/tickets", method="POST", data=b"{not json", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_13_agents_crud(self):
        self.assertEqual(call("POST", "/api/agents", {"name": "", "email": "x@y.z"})[0], 400)
        self.assertEqual(call("POST", "/api/agents", {"name": "N", "email": "bad"})[0], 400)
        self.assertEqual(call("POST", "/api/agents", {"name": "N", "email": "n@y.z", "role": "king"})[0], 400)
        code, a = call("POST", "/api/agents", {"name": "Nina New", "email": "Nina@Example.local", "role": "supervisor"})
        self.assertEqual(code, 201)
        self.assertEqual(call("POST", "/api/agents", {"name": "Dup", "email": "nina@example.local"})[0], 409)
        ag = [x for x in call("GET", "/api/agents")[1] if x["id"] == a["id"]][0]
        self.assertEqual(ag["email"], "nina@example.local"); self.assertEqual(ag["role"], "supervisor")
        self.assertEqual(call("PATCH", "/api/agents/%d" % a["id"], {"name": "Nina Renamed", "role": "admin"})[0], 200)
        self.assertEqual(call("PATCH", "/api/agents/%d" % a["id"], {"email": "sam@example.local"})[0], 409)
        self.assertEqual(call("PATCH", "/api/agents/99999", {"name": "x"})[0], 404)
        _, r = call("POST", "/api/tickets", {"subject": "Assigned to Nina", "requester_email": "q@q.q", "responder_id": a["id"]})
        code, d = call("DELETE", "/api/agents/%d" % a["id"])
        self.assertEqual(code, 200); self.assertEqual(d["unassigned_tickets"], 1)
        self.assertIsNone(call("GET", "/api/tickets/%d" % r["id"])[1]["responder_id"])
        self.assertNotIn(a["id"], [x["id"] for x in call("GET", "/api/agents")[1]])
        self.assertEqual(call("DELETE", "/api/agents/%d" % a["id"])[0], 404)

    def test_14_last_agent_protected(self):
        with app.connect() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM agents WHERE role<>'admin' ORDER BY id")]
        for i in ids:
            self.assertEqual(call("DELETE", "/api/agents/%d" % i)[0], 200)
        admin = call("GET", "/api/me")[1]["id"]
        self.assertEqual(call("DELETE", "/api/agents/%d" % admin)[0], 400)  # cannot delete yourself / the last admin
        self.assertEqual(call("PATCH", "/api/agents/%d" % admin, {"role": "agent"})[0], 400)
        self.assertEqual(len(call("GET", "/api/agents")[1]), 1)

    def test_15_phone_whatsapp(self):
        code, r = call("POST", "/api/tickets", {"subject": "WA ticket", "requester_email": "wa@x.com", "requester_phone": "082 123 4567", "source": "whatsapp"})
        self.assertEqual(code, 201)
        t = call("GET", "/api/tickets/%d" % r["id"])[1]
        self.assertEqual(t["requester_phone"], "082 123 4567"); self.assertEqual(t["source"], "whatsapp")
        self.assertEqual(call("PATCH", "/api/contacts/%d" % t["requester_id"], {"phone": "+27 82 999 0000"})[0], 200)
        self.assertEqual(call("GET", "/api/tickets/%d" % r["id"])[1]["requester_phone"], "+27 82 999 0000")
        self.assertEqual(call("PATCH", "/api/contacts/99999", {"phone": "1"})[0], 404)
        # a new ticket from the same customer updates the stored number
        _, r2 = call("POST", "/api/tickets", {"subject": "Again", "requester_email": "wa@x.com", "requester_phone": "083 000 1111"})
        self.assertEqual(call("GET", "/api/tickets/%d" % r2["id"])[1]["requester_phone"], "083 000 1111")

    def test_16_pwa_assets(self):
        code, body = call("GET", "/manifest.webmanifest")
        self.assertEqual(code, 200); self.assertEqual(json.loads(body)["display"], "standalone")
        self.assertEqual(call("GET", "/icon.svg")[0], 200)


    def test_17_agent_pin_login(self):
        app.AUTH["disabled"] = False
        app.FAILS.clear(); app.GLOBAL_FAILS.clear()
        def login(pin):
            req = urllib.request.Request(BASE + "/api/login", method="POST", data=json.dumps({"pin": pin}).encode(), headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.headers["Set-Cookie"].split(";")[0], r.headers["Set-Cookie"]
            except urllib.error.HTTPError as e:
                return e.code, None, None
        def acall(cookie, method, path, body=None):
            req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "Cookie": cookie or ""})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}") if e.headers.get_content_type() == "application/json" else None
        try:
            # no session: everything private is closed, the customer portal is open
            self.assertEqual(call("GET", "/api/tickets")[0], 401)
            self.assertEqual(call("GET", "/api/me")[0], 401)
            self.assertEqual(call("POST", "/api/tickets", {"subject": "x", "requester_email": "a@b.c"})[0], 401)
            code, page = call("GET", "/")
            self.assertEqual(code, 200); self.assertIn(b"agent PIN", page)  # redirected to login
            self.assertEqual(call("GET", "/api/portal/articles")[0], 200)
            self.assertEqual(call("GET", "/portal")[0], 200)
            # a request from loopback (e.g. via a Funnel proxy) gets no free pass
            self.assertEqual(call("GET", "/api/agents")[0], 401)
            # admin Marius signs in with the PIN from access_pin.txt
            adm_pin = app.load_pin()
            code, adm, flags = login(adm_pin)
            self.assertEqual(code, 200); self.assertIn("HttpOnly", flags)
            self.assertEqual(acall(adm, "GET", "/api/me")[1]["name"], "Marius")
            self.assertEqual(acall(adm, "GET", "/api/me")[1]["role"], "admin")
            # admin creates agents; PIN rules
            self.assertEqual(acall(adm, "POST", "/api/agents", {"name": "A", "email": "a1@x.io", "pin": "12"})[0], 400)
            self.assertEqual(acall(adm, "POST", "/api/agents", {"name": "A", "email": "a2@x.io", "pin": adm_pin})[0], 409)  # unique PINs
            code, ag = acall(adm, "POST", "/api/agents", {"name": "Agent Ann", "email": "ann@x.io", "pin": "auto"})
            self.assertEqual(code, 201); self.assertRegex(ag["pin"], r"^\d{6}$")
            lst = acall(adm, "GET", "/api/agents")[1]
            self.assertTrue(all("pin_hash" not in x and "pin_salt" not in x for x in lst))  # hashes never leave the server
            self.assertTrue([x for x in lst if x["id"] == ag["id"]][0]["has_pin"])
            # the agent signs in with her own PIN and is limited
            code, ann0, _ = login(ag["pin"])  # temporary PIN from the admin
            self.assertEqual(code, 200)
            self.assertTrue(acall(ann0, "GET", "/api/me")[1]["must_change"])
            self.assertEqual(acall(ann0, "GET", "/api/tickets")[0], 403)  # locked until she picks her own PIN
            self.assertEqual(acall(ann0, "POST", "/api/me/pin", {"pin": "12"})[0], 400)
            self.assertEqual(acall(ann0, "POST", "/api/me/pin", {"pin": adm_pin})[0], 409)
            self.assertEqual(acall(ann0, "POST", "/api/me/pin", {"pin": ag["pin"]})[0], 400)  # must differ from the temporary one
            rq = urllib.request.Request(BASE + "/api/me/pin", method="POST", data=b'{"pin":"135790"}', headers={"Content-Type": "application/json", "Cookie": ann0})
            with urllib.request.urlopen(rq) as r:
                ann = r.headers["Set-Cookie"].split(";")[0]
            self.assertEqual(login(ag["pin"])[0], 401)  # temporary PIN is dead
            self.assertEqual(acall(ann0, "GET", "/api/me")[0], 401)  # old session dead
            self.assertFalse(acall(ann, "GET", "/api/me")[1]["must_change"])
            self.assertEqual(acall(ann, "POST", "/api/me/pin", {"pin": "246810", "current": "000000"})[0], 403)  # needs current PIN
            self.assertEqual(login("135790")[0], 200)
            self.assertEqual(acall(ann, "GET", "/api/me")[1]["name"], "Agent Ann")
            self.assertEqual(acall(ann, "GET", "/api/tickets")[0], 200)
            self.assertEqual(acall(ann, "POST", "/api/agents", {"name": "X", "email": "x@x.io"})[0], 403)
            self.assertEqual(acall(ann, "PATCH", "/api/agents/%d" % ag["id"], {"role": "admin"})[0], 403)
            self.assertEqual(acall(ann, "DELETE", "/api/agents/1")[0], 403)
            self.assertEqual(acall(ann, "GET", "/api/config")[0], 403)
            self.assertEqual(acall(ann, "POST", "/api/automations", {})[0], 403)
            # replies are attributed to the signed-in agent, not whatever the client claims
            tid = acall(ann, "GET", "/api/tickets?view=all")[1][0]["id"]
            acall(ann, "POST", "/api/tickets/%d/reply" % tid, {"kind": "note", "body": "mine", "agent_id": 1})
            conv = acall(ann, "GET", "/api/tickets/%d" % tid)[1]["conversations"]
            self.assertEqual(conv[-1]["agent_id"], ag["id"])
            # admin resets her PIN: old PIN and old session stop working
            code, r = acall(adm, "PATCH", "/api/agents/%d" % ag["id"], {"pin": "654321"})
            self.assertEqual((code, r["pin"]), (200, "654321"))
            self.assertEqual(login(ag["pin"])[0], 401)
            self.assertEqual(acall(ann, "GET", "/api/tickets")[0], 401)
            code, ann2, _ = login("654321"); self.assertEqual(code, 200)
            # forged / tampered cookies are rejected
            self.assertEqual(acall("hd_sess=1.9999999999.deadbeef", "GET", "/api/tickets")[0], 401)
            # admin removes her PIN, then deletes her; sessions die
            self.assertEqual(acall(adm, "PATCH", "/api/agents/%d" % ag["id"], {"pin": ""})[0], 200)
            self.assertEqual(acall(ann2, "GET", "/api/tickets")[0], 401)
            self.assertEqual(acall(adm, "DELETE", "/api/agents/%d" % ag["id"])[0], 200)
            # admin cannot lock themselves out
            self.assertEqual(acall(adm, "PATCH", "/api/agents/1", {"pin": ""})[0], 400)
            self.assertEqual(acall(adm, "DELETE", "/api/agents/1")[0], 400)
            # logout clears the cookie
            req = urllib.request.Request(BASE + "/api/logout", method="POST", data=b"{}", headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"})
            with urllib.request.urlopen(req) as r:
                self.assertIn("Max-Age=0", r.headers["Set-Cookie"])
            # Secure flag when served through the HTTPS funnel
            req = urllib.request.Request(BASE + "/api/login", method="POST", data=json.dumps({"pin": adm_pin}).encode(), headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"})
            with urllib.request.urlopen(req) as r:
                self.assertIn("Secure", r.headers["Set-Cookie"])
            # brute-force limit (per client address, X-Forwarded-For honoured behind the proxy)
            app.FAILS.clear(); app.GLOBAL_FAILS.clear()
            codes = []
            for _ in range(6):
                rq = urllib.request.Request(BASE + "/api/login", method="POST", data=b'{"pin":"999999"}', headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.9"})
                try:
                    urllib.request.urlopen(rq); codes.append(200)
                except urllib.error.HTTPError as e:
                    codes.append(e.code)
            self.assertEqual(codes[:5], [401] * 5); self.assertEqual(codes[5], 429)
            # a different client is not locked out by that one
            rq = urllib.request.Request(BASE + "/api/login", method="POST", data=json.dumps({"pin": adm_pin}).encode(), headers={"Content-Type": "application/json", "X-Forwarded-For": "198.51.100.7"})
            with urllib.request.urlopen(rq) as r:
                self.assertEqual(r.status, 200)
        finally:
            app.AUTH["disabled"] = True
            app.FAILS.clear(); app.GLOBAL_FAILS.clear()


    def test_18_customer_ticket_link(self):
        code, r = call("POST", "/api/portal/ticket", {"requester_email": "cust@x.io", "requester_name": "Cathy", "subject": "My gate", "description": "Gate stuck"})
        self.assertEqual(code, 201); self.assertTrue(r["path"].startswith("/t/"))
        token = r["path"][3:]
        self.assertGreaterEqual(len(token), 20)
        # agent adds a public reply and a private note
        call("POST", "/api/tickets/%d/reply" % r["id"], {"kind": "reply", "body": "We are on it"})
        call("POST", "/api/tickets/%d/reply" % r["id"], {"kind": "note", "body": "SECRET internal note"})
        agent_view = call("GET", "/api/tickets/%d" % r["id"])[1]
        self.assertEqual(agent_view["access_token"], token)
        app.AUTH["disabled"] = False  # prove no sign-in is needed
        try:
            code, page = call("GET", "/t/" + token)
            self.assertEqual(code, 200); self.assertIn(b"support ticket", page)
            code, v = call("GET", "/api/portal/ticket/" + token)
            self.assertEqual(code, 200); self.assertEqual(v["subject"], "My gate")
            txt = json.dumps(v)
            self.assertNotIn("SECRET", txt); self.assertNotIn("cust@x.io", txt); self.assertNotIn("sam@example", txt)
            self.assertEqual([m["mine"] for m in v["messages"]], [True, False])
            # customer replies; a resolved ticket reopens
            app.AUTH["disabled"] = True
            call("PATCH", "/api/tickets/%d" % r["id"], {"status": 4})
            app.AUTH["disabled"] = False
            self.assertEqual(call("POST", "/api/portal/ticket/" + token + "/reply", {"body": "Still broken"})[0], 201)
            self.assertEqual(call("POST", "/api/portal/ticket/" + token + "/reply", {"body": "  "})[0], 400)
            self.assertEqual(len(call("GET", "/api/portal/ticket/" + token)[1]["messages"]), 3)
            # the agent API stays private
            self.assertEqual(call("GET", "/api/tickets/%d" % r["id"])[0], 401)
            # wrong tokens: 404, then rate-limited
            app.TOKEN_FAILS.clear()
            codes = [call("GET", "/api/portal/ticket/" + "x" * 24)[0] for _ in range(21)]
            self.assertEqual(set(codes[:20]), {404}); self.assertEqual(codes[20], 429)
        finally:
            app.AUTH["disabled"] = True
            app.TOKEN_FAILS.clear()
        self.assertEqual(call("GET", "/api/tickets/%d" % r["id"])[1]["status"], 2)  # reopened by the customer's reply


    def test_19_tasks_threads_reports_assistant(self):
        app.AUTH["disabled"] = True
        code, tk = call("POST", "/api/tasks", {"title": "Call the customer", "ticket_id": 1, "due": "2026-12-01"})
        self.assertEqual(code, 201)
        self.assertEqual(call("POST", "/api/tasks", {"title": ""})[0], 400)
        self.assertEqual(call("POST", "/api/tasks", {"title": "x", "due": "tomorrow"})[0], 400)
        self.assertEqual(call("POST", "/api/tasks", {"title": "x", "ticket_id": 9999})[0], 404)
        self.assertEqual(len(call("GET", "/api/tasks?ticket=1")[1]), 1)
        self.assertEqual(call("PATCH", "/api/tasks/%d" % tk["id"], {"done": True})[0], 200)
        self.assertEqual(call("GET", "/api/tasks?scope=open")[1], [])
        self.assertEqual(len(call("GET", "/api/tasks?scope=done")[1]), 1)
        self.assertTrue(any("task done" in a["change"] for a in call("GET", "/api/tickets/1")[1]["activity"]))
        self.assertEqual(call("DELETE", "/api/tasks/%d" % tk["id"])[0], 200)
        self.assertEqual(call("DELETE", "/api/tasks/%d" % tk["id"])[0], 404)
        # threads
        self.assertEqual(call("POST", "/api/threads", {"title": "only title"})[0], 400)
        code, th = call("POST", "/api/threads", {"title": "Gate outage plan", "body": "Who is on site?", "ticket_id": 3})
        self.assertEqual(code, 201)
        self.assertEqual(call("POST", "/api/threads/%d/reply" % th["id"], {"body": "I am"})[0], 201)
        d = call("GET", "/api/threads/%d" % th["id"])[1]
        self.assertEqual(len(d["messages"]), 2)
        self.assertEqual(call("GET", "/api/threads")[1][0]["msgs"], 2)
        self.assertEqual(call("PATCH", "/api/threads/%d" % th["id"], {"closed": 1})[0], 200)
        self.assertEqual(call("DELETE", "/api/threads/%d" % th["id"])[0], 200)
        # reports
        code, r = call("GET", "/api/reports?days=30")
        self.assertEqual(code, 200)
        self.assertEqual(len(r["daily"]), 30); self.assertIn("by_agent", r); self.assertGreaterEqual(r["backlog"], 1)
        self.assertEqual(call("GET", "/api/reports?days=abc")[1]["days"], 30)
        # assistant + portal suggestions
        call("POST", "/api/kb", {"folder": "Gates", "title": "Gate system offline troubleshooting", "body": "If the gates do not open check power and the controller.", "published": 1})
        call("POST", "/api/kb", {"folder": "Gates", "title": "Hidden draft about gates", "body": "gates gates gates controller", "published": 0})
        code, sug = call("GET", "/api/portal/suggest?q=my+gate+system+is+offline")
        self.assertEqual(code, 200); self.assertEqual([a["title"] for a in sug], ["Gate system offline troubleshooting"])
        self.assertEqual(call("GET", "/api/portal/suggest?q=zzzz")[1], [])
        code, a = call("GET", "/api/tickets/3/assist")
        self.assertEqual(code, 200); self.assertTrue(a["articles"]); self.assertIn("summary", a)
        self.assertEqual(call("GET", "/api/tickets/9999/assist")[0], 404)

    def test_20_mailboxes_and_notifications(self):
        app.AUTH["disabled"] = True
        # no mailbox yet: replies still save, with a clear message
        code, r = call("POST", "/api/tickets/1/reply", {"kind": "reply", "body": "hello"})
        self.assertEqual(code, 201); self.assertIn("not sent", r["delivery"])
        # mailbox CRUD, secrets never returned
        self.assertEqual(call("POST", "/api/mailboxes", {"name": "x"})[0], 400)
        self.assertEqual(call("POST", "/api/mailboxes", {"name": "Support", "address": "nope"})[0], 400)
        code, mb = call("POST", "/api/mailboxes", {"name": "Support", "address": "Support@Firm.test", "smtp_host": "smtp.firm.test",
                                                    "smtp_user": "support@firm.test", "smtp_password": "s3cret", "imap_host": "imap.firm.test", "imap_password": "s3cret2"})
        self.assertEqual(code, 201)
        lst = call("GET", "/api/mailboxes")[1]
        self.assertEqual(len(lst), 1); self.assertTrue(lst[0]["is_default"]); self.assertEqual(lst[0]["address"], "support@firm.test")
        self.assertNotIn("s3cret", json.dumps(lst)); self.assertTrue(lst[0]["has_smtp_password"])
        self.assertEqual(call("PATCH", "/api/mailboxes/%d" % mb["id"], {"smtp_password": "", "name": "Main"})[0], 200)  # blank keeps the password
        with app.connect() as c:
            self.assertEqual(c.execute("SELECT smtp_password FROM mailboxes").fetchone()[0], "s3cret")
        self.assertEqual(call("POST", "/api/mailboxes/test", {"which": "smtp", "smtp_host": ""})[1]["ok"], False)
        self.assertEqual(call("POST", "/api/mailboxes/test", {"which": "bad"})[0], 400)
        self.assertTrue(call("GET", "/api/config")[1]["smtp_configured"])
        # notifications: nothing is sent until ticked
        sent = []
        orig = app.send_email
        app.send_email = lambda to, subject, body, in_reply_to=None, mailbox_id=None, force_mailbox=False: (sent.append((to, subject, body)) or ("sent", "<m@x>"))
        app.NOTIFY_SYNC = True
        try:
            cfg = call("GET", "/api/notifications")[1]
            self.assertEqual([e["key"] for e in cfg["events"] if e["enabled"]], ["reply"])
            code, t = call("POST", "/api/tickets", {"subject": "Printer", "requester_email": "cust@real.com", "requester_name": "Cara Smith", "description": "jam"})
            call("PATCH", "/api/tickets/%d" % t["id"], {"status": 4})
            self.assertEqual(sent, [])
            # tick created / resolved / closed / customer_comment and customise one text
            events = {e["key"]: {"enabled": e["key"] in ("created", "resolved", "closed", "customer_comment", "agent_assigned", "reply"),
                                 "subject": e["subject"], "body": e["body"]} for e in cfg["events"]}
            events["resolved"]["subject"] = "Done: #{id} {subject}"
            self.assertEqual(call("PUT", "/api/notifications", {"include_link": False, "events": events})[0], 200)
            self.assertEqual(call("PUT", "/api/notifications", {"events": {"bogus": {}}})[0], 400)
            code, t = call("POST", "/api/tickets", {"subject": "Scanner", "requester_email": "cust2@real.com", "requester_name": "Cara Smith", "description": "x"})
            self.assertEqual(sent[-1][0], "cust2@real.com"); self.assertIn("[#%d]" % t["id"], sent[-1][1]); self.assertIn("Hi Cara", sent[-1][2])
            n = len(sent)
            call("PATCH", "/api/tickets/%d" % t["id"], {"status": 3})       # pending: not ticked
            self.assertEqual(len(sent), n)
            call("PATCH", "/api/tickets/%d" % t["id"], {"status": 4})
            self.assertEqual(sent[-1][1], "Done: #%d Scanner" % t["id"])
            call("PATCH", "/api/tickets/%d" % t["id"], {"status": 5})
            self.assertIn("closed", sent[-1][1])
            n = len(sent)
            call("PATCH", "/api/tickets/%d" % t["id"], {"status": 2})       # reopened: not ticked
            self.assertEqual(len(sent), n)
            # reply emails honour the switch
            call("POST", "/api/tickets/%d/reply" % t["id"], {"kind": "reply", "body": "on it"})
            self.assertEqual(sent[-1][0], "cust2@real.com")
            # private notes are never emailed
            n = len(sent)
            call("POST", "/api/tickets/%d/reply" % t["id"], {"kind": "note", "body": "internal"})
            self.assertEqual(len(sent), n)
            # customer comment mails the assigned agent (demo @example.local agents are skipped), real admins get it
            with app.connect() as c:
                tok = c.execute("SELECT access_token FROM tickets WHERE id=?", (t["id"],)).fetchone()[0]
                c.execute("UPDATE agents SET email='boss@firm.test' WHERE role='admin'")
            call("POST", "/api/portal/ticket/%s/reply" % tok, {"body": "any news?"})
            self.assertEqual(sent[-1][0], "boss@firm.test"); self.assertIn("any news?", sent[-1][2])
            # never mail our own mailbox address
            n = len(sent)
            call("POST", "/api/tickets", {"subject": "Loop", "requester_email": "support@firm.test", "description": "x"})
            self.assertEqual(len(sent), n)
            # email log is visible on the ticket
            self.assertTrue(call("GET", "/api/tickets/%d" % t["id"])[1]["emails"])
            self.assertTrue(call("GET", "/api/maillog")[1])
            # reply switch off
            events["reply"]["enabled"] = False
            call("PUT", "/api/notifications", {"events": events})
            n = len(sent)
            code, r = call("POST", "/api/tickets/%d/reply" % t["id"], {"kind": "reply", "body": "silent"})
            self.assertEqual(len(sent), n); self.assertIn("not emailed", r["delivery"])
        finally:
            app.send_email = orig; app.NOTIFY_SYNC = False
        self.assertEqual(call("DELETE", "/api/mailboxes/%d" % mb["id"])[0], 200)
        self.assertEqual(call("GET", "/api/mailboxes")[1], [])

    def test_21_business_hours(self):
        from datetime import datetime, timezone
        app.AUTH["disabled"] = True
        cfg = json.loads(json.dumps(app.DEFAULT_HOURS)); cfg["enabled"] = True   # Mon-Fri 08-17 at UTC+2
        # Friday 16:00 SAST = 14:00 UTC, one working hour left that day
        fri = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(app.add_work_minutes(fri, 60, cfg), datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(app.add_work_minutes(fri, 120, cfg), datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc))   # Mon 09:00 SAST
        # created on Saturday: clock starts Monday 08:00
        sat = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(app.add_work_minutes(sat, 60, cfg), datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc))
        # a public holiday on the Monday pushes it to Tuesday
        cfg["holidays"] = ["2026-10-26"]
        self.assertEqual(app.add_work_minutes(sat, 60, cfg), datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc))
        cfg["enabled"] = False
        self.assertEqual(app.add_work_minutes(sat, 60, cfg), datetime(2026, 10, 24, 11, 0, tzinfo=timezone.utc))
        # API: validation + save + recalculate
        self.assertEqual(call("PUT", "/api/hours", {"enabled": True, "days": {"0": ["17:00", "08:00"]}})[0], 400)
        self.assertEqual(call("PUT", "/api/hours", {"enabled": True, "holidays": ["tomorrow"]})[0], 400)
        code, r = call("PUT", "/api/hours", {"enabled": True, "utc_offset_mins": 120, "days": {"0": ["08:00", "17:00"], "1": ["08:00", "17:00"]},
                                             "holidays": [], "recalculate": True})
        self.assertEqual(code, 200); self.assertGreater(r["recalculated"], 0)
        h = call("GET", "/api/hours")[1]
        self.assertTrue(h["enabled"]); self.assertIsNone(h["days"]["5"])
        code, t = call("POST", "/api/tickets", {"subject": "Hours", "requester_email": "h@x.test", "priority": 4})
        got = call("GET", "/api/tickets/%d" % t["id"])[1]
        due = app.parse_iso(got["due_by"]).astimezone(timezone.utc)
        self.assertIn((due.hour + 2) % 24, range(8, 18))      # always inside working hours (SAST)
        self.assertIn(due.weekday(), (0, 1) if due.hour != 15 else (0, 1))  # only Mon/Tue are open now
        # edit SLA
        self.assertEqual(call("PUT", "/api/sla", {"policies": {"4": {"first_response_mins": 15, "resolution_mins": 120}}})[0], 200)
        self.assertEqual([x for x in call("GET", "/api/sla")[1] if x["priority"] == 4][0]["first_response_mins"], 15)
        call("PUT", "/api/hours", {"enabled": False})

    def test_22_attachments(self):
        import base64, email.message
        app.AUTH["disabled"] = True
        b64 = lambda b: base64.b64encode(b).decode()
        code, t = call("POST", "/api/tickets", {"subject": "Photos", "requester_email": "pic@x.test", "description": "see photo"})
        tid = t["id"]
        # agent reply with attachment is stored and downloadable
        code, r = call("POST", "/api/tickets/%d/reply" % tid, {"kind": "reply", "body": "here you go",
                       "attachments": [{"name": "..\\..\\quote.pdf", "type": "application/pdf", "data": b64(b"%PDF-1.4 hello")}]})
        self.assertEqual(code, 201)
        d = call("GET", "/api/tickets/%d" % tid)[1]
        atts = [a for cv in d["conversations"] for a in cv["attachments"]]
        self.assertEqual(len(atts), 1); self.assertEqual(atts[0]["filename"], "quote.pdf")
        code, raw = call("GET", "/api/attachments/%d" % atts[0]["id"])
        self.assertEqual(code, 200)
        self.assertEqual(raw, b"%PDF-1.4 hello")
        # blocked / empty / too many / too big
        self.assertEqual(call("POST", "/api/tickets/%d/reply" % tid, {"kind": "reply", "body": "x", "attachments": [{"name": "a.exe", "data": b64(b"MZ")}]})[0], 400)
        self.assertEqual(call("POST", "/api/tickets/%d/reply" % tid, {"kind": "reply", "body": "x", "attachments": [{"name": "a.txt", "data": ""}]})[0], 400)
        self.assertEqual(call("POST", "/api/tickets/%d/reply" % tid, {"kind": "reply", "body": "x", "attachments": [{"name": "%d.txt" % i, "data": b64(b"x")} for i in range(6)]})[0], 400)
        self.assertEqual(call("POST", "/api/tickets/%d/reply" % tid, {"kind": "reply", "body": "x", "attachments": [{"name": "big.bin", "data": b64(b"x" * (11 * 1024 * 1024))}]})[0], 400)
        # private-note attachments are hidden from the customer; public ones are visible and downloadable by token
        call("POST", "/api/tickets/%d/reply" % tid, {"kind": "note", "body": "internal", "attachments": [{"name": "secret.txt", "data": b64(b"shh")}]})
        with app.connect() as c:
            tok = c.execute("SELECT access_token FROM tickets WHERE id=?", (tid,)).fetchone()[0]
        v = call("GET", "/api/portal/ticket/%s" % tok)[1]
        names = [a["filename"] for m in v["messages"] for a in m["attachments"]]
        self.assertEqual(names, ["quote.pdf"])
        code, raw = call("GET", "/api/portal/ticket/%s/file/%d" % (tok, atts[0]["id"]))
        self.assertEqual((code, raw), (200, b"%PDF-1.4 hello"))
        secret = [a for cv in call("GET", "/api/tickets/%d" % tid)[1]["conversations"] for a in cv["attachments"] if a["filename"] == "secret.txt"][0]
        self.assertEqual(call("GET", "/api/portal/ticket/%s/file/%d" % (tok, secret["id"]))[0], 404)
        # another ticket's token cannot fetch this file
        t2 = call("POST", "/api/tickets", {"subject": "Other", "requester_email": "o@x.test"})[1]["id"]
        with app.connect() as c:
            tok2 = c.execute("SELECT access_token FROM tickets WHERE id=?", (t2,)).fetchone()[0]
        self.assertEqual(call("GET", "/api/portal/ticket/%s/file/%d" % (tok2, atts[0]["id"]))[0], 404)
        # download headers force safe handling
        req = urllib.request.Request(BASE + "/api/attachments/%d" % secret["id"])
        with urllib.request.urlopen(req) as r:
            self.assertIn("attachment", r.headers["Content-Disposition"]); self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        req = urllib.request.Request(BASE + "/api/attachments/%d" % atts[0]["id"], headers={"User-Agent": "Mozilla HelpdeskApp/1.0"})
        with urllib.request.urlopen(req) as r:
            self.assertIn("attachment", r.headers["Content-Disposition"])   # PDF is inline in a browser but a download in the app
        req = urllib.request.Request(BASE + "/api/attachments/%d" % atts[0]["id"])
        with urllib.request.urlopen(req) as r:
            self.assertIn("inline", r.headers["Content-Disposition"])
        # customer uploads from the ticket page (limit 3 files / 5 MB)
        code, _ = call("POST", "/api/portal/ticket/%s/reply" % tok, {"body": "my photo", "attachments": [{"name": "p.png", "type": "image/png", "data": b64(b"\x89PNG....")}]})
        self.assertEqual(code, 201)
        self.assertEqual(call("POST", "/api/portal/ticket/%s/reply" % tok, {"body": "x", "attachments": [{"name": "b.bin", "data": b64(b"x" * (6 * 1024 * 1024))}]})[0], 400)
        # new request from the portal with a file
        code, j = call("POST", "/api/portal/ticket", {"requester_email": "p@x.test", "subject": "Broken", "description": "see file",
                                                    "attachments": [{"name": "log.txt", "data": b64(b"error 42")}]})
        self.assertEqual(code, 201)
        self.assertEqual(sum(len(cv["attachments"]) for cv in call("GET", "/api/tickets/%d" % j["id"])[1]["conversations"]), 1)
        # inbound email with an attachment (and a blocked one)
        m = email.message.EmailMessage()
        m["From"] = "Cust <cust@x.test>"; m["To"] = "support@firm.test"; m["Subject"] = "Invoice attached"; m["Message-ID"] = "<att1@x.test>"
        m.set_content("please see invoice")
        m.add_attachment(b"%PDF-1.4 inv", maintype="application", subtype="pdf", filename="invoice.pdf")
        m.add_attachment(b"MZ..", maintype="application", subtype="octet-stream", filename="run.exe")
        with app.connect() as c:
            tid3 = app.ingest_email(c, m.as_bytes())
        d3 = call("GET", "/api/tickets/%d" % tid3)[1]
        self.assertEqual([a["filename"] for cv in d3["conversations"] for a in cv["attachments"]], ["invoice.pdf"])
        self.assertIn("run.exe", d3["conversations"][0]["body"])
        # outgoing mail carries the attachment
        sent = []
        orig = app.send_email
        ev = {e["key"]: e for e in call("GET", "/api/notifications")[1]["events"]}
        ev["reply"]["enabled"] = True
        call("PUT", "/api/notifications", {"events": ev})
        app.send_email = lambda *a, **k: (sent.append(k.get("attachments")) or ("sent", "<m>"))
        try:
            call("POST", "/api/tickets/%d/reply" % tid3, {"kind": "reply", "body": "got it", "attachments": [{"name": "r.txt", "data": b64(b"receipt")}]})
        finally:
            app.send_email = orig
        self.assertEqual(sent[0][0][0], "r.txt")

    def test_23_whatsapp_log(self):
        app.AUTH["disabled"] = True
        t = call("POST", "/api/tickets", {"subject": "WA", "requester_email": "wa@x.test", "description": "hi"})[1]["id"]
        self.assertEqual(call("POST", "/api/tickets/%d/whatsapp" % t, {"to": "customer", "text": ""})[0], 400)
        self.assertEqual(call("POST", "/api/tickets/%d/whatsapp" % t, {"to": "bogus", "text": "x"})[0], 400)
        self.assertEqual(call("POST", "/api/tickets/%d/whatsapp" % t, {"to": "customer", "text": "We are on our way"})[0], 201)
        d = call("GET", "/api/tickets/%d" % t)[1]
        last = d["conversations"][-1]
        self.assertEqual(last["kind"], "reply"); self.assertIn("WhatsApp", last["delivery"]); self.assertTrue(d["first_responded_at"])
        self.assertEqual(call("POST", "/api/tickets/%d/whatsapp" % t, {"to": "customer_in", "text": "Thanks, see you"})[0], 201)
        self.assertEqual(call("GET", "/api/tickets/%d" % t)[1]["conversations"][-1]["kind"], "incoming")
        self.assertEqual(call("POST", "/api/tickets/%d/whatsapp" % t, {"to": "agent", "text": "x", "name": "Sam"})[0], 201)
        self.assertTrue(any("WhatsApp" in a["change"] for a in call("GET", "/api/tickets/%d" % t)[1]["activity"]))

if __name__ == "__main__":
    unittest.main(verbosity=2)
