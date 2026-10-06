import os, json, uuid, smtplib, sqlite3
from datetime import date
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, flash, g, session, abort
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
from i18n import T, CATS

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")
DB = "portal.db"
UP = os.path.join(app.static_folder, "uploads")
THRESH = 0.45


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d: d.close()


def init_db():
    d = sqlite3.connect(DB)
    d.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE,
        pw TEXT, role TEXT DEFAULT 'user', trust INTEGER DEFAULT 50);
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, name TEXT,
        category TEXT, place TEXT, lat REAL, lng REAL, item_date TEXT, description TEXT,
        photo TEXT, feat TEXT, status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY, item_id INTEGER, user_id INTEGER,
        proof TEXT, status TEXT DEFAULT 'pending', token TEXT);
    CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER, msg TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP);""")
    if not d.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        d.execute("INSERT INTO users(name,email,pw,role,trust) VALUES(?,?,?,?,?)",
                  ("Admin", "admin@s3.edu", generate_password_hash("admin123"), "admin", 100))
    d.commit(); d.close()


# ---------- helpers ----------
@app.context_processor
def ctx():
    lang = session.get("lang", "en")
    return dict(t=lambda k: T[lang].get(k, T["en"].get(k, k)), lang=lang, cats=CATS, me=me())


def me():
    uid = session.get("uid")
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None


def need_login():
    if not session.get("uid"): abort(redirect(url_for("login")))


def need_admin():
    u = me()
    if not u or u["role"] != "admin": abort(403)


def features(path):
    """Image features: 8x8 average-hash bits + 4-bin RGB histogram (basic CV)."""
    img = Image.open(path).convert("RGB")
    px = list(img.convert("L").resize((8, 8)).getdata()); avg = sum(px) / 64
    hist = []
    for ch in img.resize((32, 32)).split():
        d = list(ch.getdata())
        hist += [sum(1 for v in d if v // 64 == b) / len(d) for b in range(4)]
    return json.dumps({"b": [int(p > avg) for p in px], "h": hist})


def img_sim(f1, f2):
    a, b = json.loads(f1), json.loads(f2)
    ham = sum(x != y for x, y in zip(a["b"], b["b"])) / 64
    inter = sum(min(x, y) for x, y in zip(a["h"], b["h"])) / 3
    return 0.5 * (1 - ham) + 0.5 * inter


def score(a, b):
    wa, wb = set(a["name"].lower().split()), set(b["name"].lower().split())
    name = len(wa & wb) / max(1, len(wa | wb))
    cat = 1.0 if a["category"] == b["category"] else 0.0
    if a["feat"] and b["feat"]:
        return 0.4 * img_sim(a["feat"], b["feat"]) + 0.35 * name + 0.25 * cat
    return 0.6 * name + 0.4 * cat


def matches_for(item):
    other = "found" if item["type"] == "lost" else "lost"
    rows = db().execute("SELECT * FROM items WHERE type=? AND status='approved'", (other,)).fetchall()
    res = sorted(((score(item, r), r) for r in rows), key=lambda x: -x[0])
    return [(s, r) for s, r in res if s >= THRESH]


def notify(uid, msg):
    db().execute("INSERT INTO notifications(user_id,msg) VALUES(?,?)", (uid, msg))
    host = os.environ.get("SMTP_HOST")  # optional real email alerts
    u = db().execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    if host and u:
        try:
            m = EmailMessage(); m["Subject"] = "Lost and Found alert"
            m["From"] = os.environ.get("SMTP_FROM", "portal@s3.edu"); m["To"] = u["email"]; m.set_content(msg)
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 25))) as s: s.send_message(m)
        except Exception as e:
            print("email failed:", e)


def trust(uid, delta):
    db().execute("UPDATE users SET trust=MAX(0,MIN(100,trust+?)) WHERE id=?", (delta, uid))


# ---------- routes ----------
@app.route("/lang/<l>")
def lang(l):
    if l in T: session["lang"] = l
    return redirect(request.referrer or url_for("index"))


@app.route("/")
def index():
    q, ty, c = request.args.get("q", "").strip(), request.args.get("type", ""), request.args.get("category", "")
    d1, d2 = request.args.get("from", ""), request.args.get("to", "")
    sql, a = "SELECT * FROM items WHERE status IN ('approved','claimed')", []
    if q: sql += " AND (name LIKE ? OR place LIKE ? OR description LIKE ?)"; a += [f"%{q}%"] * 3
    if ty in ("lost", "found"): sql += " AND type=?"; a.append(ty)
    if c: sql += " AND category=?"; a.append(c)
    if d1: sql += " AND item_date>=?"; a.append(d1)
    if d2: sql += " AND item_date<=?"; a.append(d2)
    return render_template("index.html", items=db().execute(sql + " ORDER BY id DESC", a).fetchall(), q=q, ty=ty, c=c, d1=d1, d2=d2)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        try:
            db().execute("INSERT INTO users(name,email,pw) VALUES(?,?,?)",
                         (f["name"].strip(), f["email"].strip().lower(), generate_password_hash(f["pw"])))
            db().commit(); flash("Registered. Please login."); return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Email already registered.")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].strip().lower(),)).fetchone()
        if u and check_password_hash(u["pw"], request.form["pw"]):
            session["uid"] = u["id"]; return redirect(url_for("index"))
        flash("Wrong email or password.")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.pop("uid", None); return redirect(url_for("index"))


@app.route("/report", methods=["GET", "POST"])
def report():
    need_login()
    if request.method == "POST":
        f = request.form
        if not f["name"].strip() or not f["place"].strip():
            flash("Item name and place are required.")
        else:
            photo = feat = None
            p = request.files.get("photo")
            if p and p.filename:
                photo = uuid.uuid4().hex + ".jpg"
                try:
                    Image.open(p.stream).convert("RGB").save(os.path.join(UP, photo), "JPEG")
                    feat = features(os.path.join(UP, photo))
                except Exception:
                    photo = feat = None; flash("Photo could not be read, saved without it.")
            lat = float(f["lat"]) if f.get("lat") else None
            lng = float(f["lng"]) if f.get("lng") else None
            db().execute("INSERT INTO items(user_id,type,name,category,place,lat,lng,item_date,description,photo,feat) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (session["uid"], f["type"], f["name"].strip(), f["category"], f["place"].strip(), lat, lng,
                          f["item_date"], f["description"].strip(), photo, feat))
            db().commit(); flash("Saved. Waiting for admin approval."); return redirect(url_for("index"))
    return render_template("report.html", today=date.today())


@app.route("/item/<int:i>")
def item(i):
    it = db().execute("SELECT i.*, u.name uname, u.trust utrust FROM items i JOIN users u ON u.id=i.user_id WHERE i.id=?", (i,)).fetchone() or abort(404)
    ms = matches_for(it) if it["status"] == "approved" else []
    return render_template("item.html", it=it, ms=ms)


@app.route("/claim/<int:i>", methods=["POST"])
def claim(i):
    need_login()
    db().execute("INSERT INTO claims(item_id,user_id,proof) VALUES(?,?,?)", (i, session["uid"], request.form["proof"].strip()))
    db().commit(); flash("Claim sent for review."); return redirect(url_for("item", i=i))


@app.route("/alerts")
def alerts():
    need_login()
    ns = db().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (session["uid"],)).fetchall()
    cl = db().execute("SELECT c.*, i.name FROM claims c JOIN items i ON i.id=c.item_id WHERE c.user_id=? ORDER BY c.id DESC", (session["uid"],)).fetchall()
    return render_template("alerts.html", ns=ns, cl=cl)


@app.route("/admin")
def admin():
    need_admin(); d = db()
    pend = d.execute("SELECT * FROM items WHERE status='pending'").fetchall()
    cls = d.execute("SELECT c.*, i.name, u.name uname, u.trust FROM claims c JOIN items i ON i.id=c.item_id JOIN users u ON u.id=c.user_id WHERE c.status='pending'").fetchall()
    bycat = d.execute("SELECT category k, COUNT(*) n FROM items GROUP BY category").fetchall()
    byplace = d.execute("SELECT place k, COUNT(*) n FROM items GROUP BY place ORDER BY n DESC LIMIT 6").fetchall()
    tot = d.execute("SELECT SUM(type='lost') l, SUM(type='found') f, SUM(status='claimed') c FROM items").fetchone()
    return render_template("admin.html", pend=pend, cls=cls, bycat=[dict(r) for r in bycat], byplace=[dict(r) for r in byplace], tot=tot)


@app.route("/admin/item/<int:i>/<act>")
def admin_item(i, act):
    need_admin(); d = db()
    it = d.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone() or abort(404)
    d.execute("UPDATE items SET status=? WHERE id=?", ("approved" if act == "approve" else "rejected", i))
    if act == "approve":
        d.commit(); it = d.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone()
        for s, r in matches_for(it):  # automatic alerts to both sides of a match
            pct = int(s * 100)
            notify(r["user_id"], f"New item '{it['name']}' matches your '{r['name']}' ({pct}% match). Open item #{it['id']}.")
            notify(it["user_id"], f"Your item '{it['name']}' matches '{r['name']}' ({pct}% match). Open item #{r['id']}.")
    else:
        trust(it["user_id"], -10)  # fake / bad report
    d.commit(); return redirect(url_for("admin"))


@app.route("/admin/claim/<int:i>/<act>")
def admin_claim(i, act):
    need_admin(); d = db()
    c = d.execute("SELECT c.*, i.user_id owner FROM claims c JOIN items i ON i.id=c.item_id WHERE c.id=?", (i,)).fetchone() or abort(404)
    if act == "approve":
        d.execute("UPDATE claims SET status='approved', token=? WHERE id=?", (uuid.uuid4().hex[:12], i))
        d.execute("UPDATE items SET status='claimed' WHERE id=?", (c["item_id"],))
        trust(c["user_id"], 5); trust(c["owner"], 10)  # genuine return rewards the reporter
        notify(c["user_id"], "Your claim was approved. Open Alerts to get your QR pass.")
    else:
        d.execute("UPDATE claims SET status='rejected' WHERE id=?", (i,)); trust(c["user_id"], -15)
        notify(c["user_id"], "Your claim was rejected.")
    d.commit(); return redirect(url_for("admin"))


@app.route("/verify/<token>")
def verify(token):
    need_admin()
    c = db().execute("SELECT * FROM claims WHERE token=? AND status='approved'", (token,)).fetchone()
    if c: db().execute("UPDATE claims SET status='done' WHERE id=?", (c["id"],)); db().commit()
    return render_template("verify.html", ok=bool(c))


init_db()
if __name__ == "__main__":
    app.run(debug=True)
