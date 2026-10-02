import os, sqlite3, secrets, time, mimetypes, uuid
from io import BytesIO
import qrcode
from functools import wraps
from urllib.parse import urljoin, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json, base64
from flask import Flask, request, session, redirect, url_for, render_template, jsonify, send_from_directory, send_file

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
DB = os.path.join(APP_DIR, "tmd_chat.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 180

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,phone TEXT UNIQUE NOT NULL,name TEXT NOT NULL,avatar TEXT DEFAULT '',role TEXT DEFAULT 'member',org_type TEXT DEFAULT 'company',org_name TEXT DEFAULT '',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS otp_codes(phone TEXT PRIMARY KEY,code TEXT NOT NULL,expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'group',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chat_members(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,joined_at INTEGER NOT NULL,PRIMARY KEY(chat_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,body TEXT NOT NULL DEFAULT '',message_type TEXT NOT NULL DEFAULT 'text',file_name TEXT DEFAULT '',file_url TEXT DEFAULT '',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,is_read INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS contacts(user_id INTEGER NOT NULL,contact_user_id INTEGER NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(user_id,contact_user_id));
    CREATE TABLE IF NOT EXISTS linked_devices(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,token TEXT UNIQUE NOT NULL,device_name TEXT DEFAULT 'Computer',confirmed INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,last_seen INTEGER NOT NULL);
    """)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(linked_devices)").fetchall()}
    if "device_name" not in cols: con.execute("ALTER TABLE linked_devices ADD COLUMN device_name TEXT DEFAULT 'Computer'")
    if "confirmed" not in cols: con.execute("ALTER TABLE linked_devices ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
    if "expires_at" not in cols: con.execute("ALTER TABLE linked_devices ADD COLUMN expires_at INTEGER DEFAULT 0")
    now = int(time.time())
    if con.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0:
        con.execute("INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)",("Team Discussion","group",now))
        con.execute("INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)",("Company Announcements","announcement",now))
    con.commit(); con.close()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        uid = session.get("user_id")
        if not uid:
            return redirect(url_for("login"))
        con = db()
        user = con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        con.close()
        if user is None:
            session.clear()
            return redirect(url_for("login"))
        return fn(*args,**kwargs)
    return wrapper

def ensure_default_membership(con,user_id):
    rows=con.execute("SELECT id FROM chats WHERE kind IN ('group','announcement')").fetchall()
    now=int(time.time())
    for row in rows:
        con.execute("INSERT OR IGNORE INTO chat_members(chat_id,user_id,joined_at) VALUES(?,?,?)",(row["id"],user_id,now))

def current_user(con):
    uid = session.get("user_id")
    if not uid:
        return None
    return con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()

@app.route("/")
def index(): return redirect(url_for("home") if "user_id" in session else url_for("login"))

def normalize_phone(raw):
    phone=(raw or "").strip().replace(" ","").replace("-","").replace("(","").replace(")","")
    if phone.startswith("+"): phone=phone[1:]
    if phone.isdigit() and len(phone)==10:
        phone="91"+phone
    return phone

def messagecentral_enabled():
    # Message Central console provides a ready-to-use Auth Token.
    # Keep the key-based token generation as a fallback for accounts that use it.
    return bool(os.environ.get("MC_CUSTOMER_ID") and (os.environ.get("MC_AUTH_TOKEN") or (os.environ.get("MC_EMAIL") and os.environ.get("MC_KEY"))))

def messagecentral_token():
    direct_token=os.environ.get("MC_AUTH_TOKEN")
    if direct_token:
        return direct_token.strip()

    params=urlencode({
        "customerId":os.environ["MC_CUSTOMER_ID"],
        "key":base64.b64encode(os.environ["MC_KEY"].encode("utf-8")).decode("ascii"),
        "scope":"NEW",
        "country":os.environ.get("MC_COUNTRY","91"),
        "email":os.environ["MC_EMAIL"],
    })
    req=Request(
        "https://cpaas.messagecentral.com/auth/v1/authentication/token?"+params,
        headers={"accept":"*/*"},
        method="GET",
    )
    with urlopen(req,timeout=20) as resp:
        data=json.loads(resp.read().decode("utf-8"))
    token=data.get("token")
    if not token:
        raise RuntimeError(data.get("message") or "Message Central token generation failed")
    return token

def messagecentral_send_otp(phone):
    country=os.environ.get("MC_COUNTRY","91")
    mobile=phone[len(country):] if phone.startswith(country) and len(phone)>len(country) else phone
    params=urlencode({
        "countryCode":country,
        "customerId":os.environ["MC_CUSTOMER_ID"],
        "flowType":"SMS",
        "mobileNumber":mobile,
        "otpLength":"6",
    })
    token=messagecentral_token()
    req=Request(
        "https://cpaas.messagecentral.com/verification/v3/send?"+params,
        data=b"",
        headers={"authToken":token,"accept":"*/*"},
        method="POST",
    )
    try:
        with urlopen(req,timeout=20) as resp:
            data=json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw=exc.read().decode("utf-8","replace")
        try:
            data=json.loads(raw)
        except ValueError:
            data={}
        code=data.get("responseCode") or data.get("data",{}).get("responseCode") or exc.code
        message=data.get("message") or data.get("data",{}).get("errorMessage") or "OTP could not be sent"
        raise RuntimeError(f"Message Central error {code}: {message}")
    if str(data.get("responseCode")) != "200":
        code=data.get("responseCode") or data.get("data",{}).get("responseCode") or "UNKNOWN"
        message=data.get("message") or data.get("data",{}).get("errorMessage") or "OTP could not be sent"
        raise RuntimeError(f"Message Central error {code}: {message}")
    verification_id=data.get("data",{}).get("verificationId")
    if not verification_id:
        raise RuntimeError("Message Central did not return a verification ID")
    return verification_id


def messagecentral_verify_otp(verification_id, code):
    country=os.environ.get("MC_COUNTRY","91")
    phone=session.get("pending_phone","")
    mobile=phone[len(country):] if phone.startswith(country) and len(phone)>len(country) else phone
    token=messagecentral_token()
    params=urlencode({
        "countryCode":country,
        "customerId":os.environ["MC_CUSTOMER_ID"],
        "mobileNumber":mobile,
        "verificationId":verification_id,
        "code":code,
    })
    req=Request(
        "https://cpaas.messagecentral.com/verification/v3/validateOtp?"+params,
        headers={"authToken":token,"accept":"*/*"},
        method="GET",
    )
    try:
        with urlopen(req,timeout=20) as resp:
            data=json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw=exc.read().decode("utf-8","replace")
        try:
            data=json.loads(raw)
        except ValueError:
            data={}
        code_value=data.get("responseCode") or data.get("data",{}).get("responseCode") or exc.code
        message=data.get("message") or data.get("data",{}).get("errorMessage") or "OTP verification failed"
        raise RuntimeError(f"Message Central error {code_value}: {message}")
    response_code=str(data.get("responseCode"))
    status=str(data.get("data",{}).get("verificationStatus","")).upper()
    if response_code=="200" and status in ("VERIFICATION_COMPLETED","VERIFIED"):
        return True
    code_value=data.get("responseCode") or data.get("data",{}).get("responseCode") or "UNKNOWN"
    message=data.get("message") or data.get("data",{}).get("errorMessage") or "OTP verification failed"
    raise RuntimeError(f"Message Central error {code_value}: {message}")


@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        phone=normalize_phone(request.form.get("phone",""))
        if not phone.isdigit() or len(phone)<10 or len(phone)>15:
            return render_template("login.html",error="Please enter a valid mobile number.")
        session.permanent=True
        session["pending_phone"]=phone

        if messagecentral_enabled():
            try:
                verification_id=messagecentral_send_otp(phone)
                session["mc_verification_id"]=str(verification_id)
                session.pop("dev_otp",None)
                return render_template("otp.html")
            except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                return render_template("login.html",error=str(exc) or "OTP service could not send the SMS.")
        else:
            code=os.environ.get("DEV_OTP") or f"{secrets.randbelow(1000000):06d}"
            con=db(); con.execute("REPLACE INTO otp_codes(phone,code,expires_at) VALUES(?,?,?)",(phone,code,int(time.time())+300)); con.commit(); con.close()
            session["dev_otp"]=code
            return render_template("otp.html",dev_otp=code if os.environ.get("DEV_OTP") else None)
    return render_template("login.html")

@app.route("/verify",methods=["POST"])
def verify():
    phone=session.get("pending_phone"); code=request.form.get("code","").strip()
    if not phone: return redirect(url_for("login"))

    if session.get("mc_verification_id"):
        try:
            verified=messagecentral_verify_otp(session["mc_verification_id"],code)
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return render_template("otp.html",error=str(exc) or "OTP verification failed.")
    else:
        con=db(); row=con.execute("SELECT * FROM otp_codes WHERE phone=?",(phone,)).fetchone()
        if not row or row["expires_at"]<int(time.time()) or row["code"]!=code:
            con.close(); return render_template("otp.html",error="Invalid or expired OTP.")

    con=db(); user=con.execute("SELECT * FROM users WHERE phone=?",(phone,)).fetchone()
    if user:
        session["user_id"]=user["id"]
        session.permanent=True
        session.pop("pending_phone",None); session.pop("mc_verification_id",None); session.pop("dev_otp",None)
        ensure_default_membership(con,user["id"]); con.commit(); con.close()
        return redirect(url_for("home"))
    session["verified_phone"]=phone
    session.pop("pending_phone",None); session.pop("mc_verification_id",None); session.pop("dev_otp",None)
    con.close()
    return redirect(url_for("profile_setup"))

@app.route("/profile-setup",methods=["GET","POST"])
def profile_setup():
    phone=session.get("verified_phone")
    if not phone: return redirect(url_for("login"))
    if request.method=="POST":
        name=request.form.get("name","").strip(); org_type=request.form.get("org_type","company"); org_name=request.form.get("org_name","").strip()
        if not name or not org_name: return render_template("setup.html",error="Name and company/shop/team are required.")
        con=db(); cur=con.execute("INSERT INTO users(phone,name,role,org_type,org_name,created_at) VALUES(?,?,?,?,?,?)",(phone,name,"member",org_type,org_name,int(time.time()))); uid=cur.lastrowid
        ensure_default_membership(con,uid); con.commit(); con.close(); session["user_id"]=uid; session.permanent=True; session.pop("verified_phone",None)
        return redirect(url_for("home"))
    return render_template("setup.html",phone=phone)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/home")
@login_required
def home():
    con=db()
    user=current_user(con)
    if user is None:
        con.close()
        session.clear()
        return redirect(url_for("login"))
    ensure_default_membership(con,user["id"])
    chats=con.execute("""SELECT c.*,COALESCE((SELECT body FROM messages m WHERE m.chat_id=c.id ORDER BY m.id DESC LIMIT 1),'No messages yet') last_message,(SELECT COUNT(*) FROM chat_members cm WHERE cm.chat_id=c.id) member_count,(SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) message_count FROM chats c JOIN chat_members me ON me.chat_id=c.id AND me.user_id=? ORDER BY COALESCE((SELECT MAX(m2.id) FROM messages m2 WHERE m2.chat_id=c.id),0) DESC,c.id DESC""",(user["id"],)).fetchall()
    users=con.execute("SELECT id,name,phone,org_name,org_type FROM users WHERE id<>? ORDER BY name",(user["id"],)).fetchall()
    contacts=con.execute("SELECT u.id,u.name,u.phone,u.org_name,u.org_type FROM contacts c JOIN users u ON u.id=c.contact_user_id WHERE c.user_id=? ORDER BY u.name",(user["id"],)).fetchall()
    unread=con.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",(user["id"],)).fetchone()[0]
    con.commit(); con.close(); return render_template("home.html",user=user,chats=chats,users=users,contacts=contacts,unread=unread)

@app.route("/chat/<int:chat_id>")
@login_required
def chat(chat_id):
    con=db(); user=current_user(con)
    if user is None:
        con.close(); session.clear(); return redirect(url_for("login"))
    chat=con.execute("SELECT * FROM chats WHERE id=?",(chat_id,)).fetchone()
    member=con.execute("SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?",(chat_id,user["id"])).fetchone()
    if not chat or not member: con.close(); return redirect(url_for("home"))
    msgs=con.execute("SELECT messages.*,users.name FROM messages JOIN users ON users.id=messages.user_id WHERE chat_id=? ORDER BY messages.id",(chat_id,)).fetchall()
    members=con.execute("SELECT users.id,users.name,users.phone,users.org_name FROM chat_members JOIN users ON users.id=chat_members.user_id WHERE chat_members.chat_id=? ORDER BY users.name",(chat_id,)).fetchall()
    con.close(); return render_template("chat.html",user=user,chat=chat,messages=msgs,members=members)

@app.post("/api/contacts")
@login_required
def add_contact():
    data=request.get_json() or {}
    phone=normalize_phone(data.get("phone",""))
    if not phone.isdigit() or len(phone)<10 or len(phone)>15:
        return jsonify(error="Please enter a valid mobile number."),400
    con=db()
    other=con.execute("SELECT id,name,phone,org_name,org_type FROM users WHERE phone=?",(phone,)).fetchone()
    if not other:
        con.close()
        return jsonify(error="No TMD Chat user found with this mobile number. They must sign up first."),404
    if other["id"]==session["user_id"]:
        con.close()
        return jsonify(error="You cannot add your own number."),400
    con.execute("INSERT OR IGNORE INTO contacts(user_id,contact_user_id,created_at) VALUES(?,?,?)",(session["user_id"],other["id"],int(time.time())))
    con.commit()
    contact=dict(other)
    con.close()
    return jsonify(ok=True,contact=contact)

@app.get("/api/contacts")
@login_required
def contacts_api():
    con=db()
    rows=con.execute("SELECT u.id,u.name,u.phone,u.org_name,u.org_type FROM contacts c JOIN users u ON u.id=c.contact_user_id WHERE c.user_id=? ORDER BY u.name",(session["user_id"],)).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/direct-chat")
@login_required
def direct_chat():
    data=request.get_json() or {}
    try: other_id=int(data.get("user_id"))
    except (TypeError,ValueError): return jsonify(error="User not found"),404
    con=db(); other=con.execute("SELECT * FROM users WHERE id=?",(other_id,)).fetchone()
    if not other or other["id"]==session["user_id"]: con.close(); return jsonify(error="User not found"),404
    existing=con.execute("""SELECT c.id FROM chats c JOIN chat_members a ON a.chat_id=c.id JOIN chat_members b ON b.chat_id=c.id WHERE c.kind='direct' AND a.user_id=? AND b.user_id=? GROUP BY c.id HAVING COUNT(*)=2""",(session["user_id"],other_id)).fetchone()
    if existing: con.close(); return jsonify(ok=True,chat_id=existing["id"],redirect=url_for("chat",chat_id=existing["id"]))
    cur=con.execute("INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)",(other["name"],"direct",int(time.time()))); chat_id=cur.lastrowid; now=int(time.time())
    con.executemany("INSERT INTO chat_members(chat_id,user_id,joined_at) VALUES(?,?,?)",[(chat_id,session["user_id"],now),(chat_id,other_id,now)]); con.commit(); con.close()
    return jsonify(ok=True,chat_id=chat_id,redirect=url_for("chat",chat_id=chat_id))

@app.post("/api/chats")
@login_required
def create_chat():
    data=request.get_json() or {}; title=(data.get("title") or "").strip(); kind=data.get("kind","group"); member_ids=data.get("member_ids") or []
    if not title: return jsonify(error="Chat name is required"),400
    if kind not in ("group","announcement"): kind="group"
    con=db(); cur=con.execute("INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)",(title,kind,int(time.time()))); chat_id=cur.lastrowid
    ids={session["user_id"]}
    for raw in member_ids:
        try: ids.add(int(raw))
        except (TypeError,ValueError): pass
    now=int(time.time()); con.executemany("INSERT OR IGNORE INTO chat_members(chat_id,user_id,joined_at) VALUES(?,?,?)",[(chat_id,uid,now) for uid in ids]); con.commit(); con.close()
    return jsonify(ok=True,chat_id=chat_id,redirect=url_for("chat",chat_id=chat_id))

@app.post("/api/chats/<int:chat_id>/members")
@login_required
def add_member(chat_id):
    data=request.get_json() or {}
    try: uid=int(data.get("user_id"))
    except (TypeError,ValueError): return jsonify(error="User not found"),404
    con=db(); chat=con.execute("SELECT id FROM chats WHERE id=?",(chat_id,)).fetchone(); user=con.execute("SELECT id FROM users WHERE id=?",(uid,)).fetchone(); is_member=con.execute("SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?",(chat_id,session["user_id"])).fetchone()
    if not chat or not user or not is_member: con.close(); return jsonify(error="Chat or user not found"),404
    con.execute("INSERT OR IGNORE INTO chat_members VALUES(?,?,?)",(chat_id,uid,int(time.time()))); con.commit(); con.close(); return jsonify(ok=True)

@app.post("/api/messages")
@login_required
def send_message():
    data=request.get_json() or {}; body=(data.get("body") or "").strip()
    try: chat_id=int(data.get("chat_id"))
    except (TypeError,ValueError): return jsonify(error="Chat is required"),400
    message_type=data.get("message_type","text"); file_name=(data.get("file_name") or "").strip(); file_url=(data.get("file_url") or "").strip()
    if message_type not in ("text","image","document","voice"): message_type="text"
    if not body and not file_url: return jsonify(error="Message or attachment is required"),400
    con=db(); member=con.execute("SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?",(chat_id,session["user_id"])).fetchone()
    if not member: con.close(); return jsonify(error="You are not a member of this chat"),403
    cur=con.execute("INSERT INTO messages(chat_id,user_id,body,message_type,file_name,file_url,created_at) VALUES(?,?,?,?,?,?,?)",(chat_id,session["user_id"],body,message_type,file_name,file_url,int(time.time())))
    others=con.execute("SELECT user_id FROM chat_members WHERE chat_id=? AND user_id<>?",(chat_id,session["user_id"])).fetchall()
    for row in others: con.execute("INSERT INTO notifications(user_id,title,body,is_read,created_at) VALUES(?,?,?,?,?)",(row["user_id"],"New message",body[:120] or file_name or "Attachment",0,int(time.time())))
    con.commit(); mid=cur.lastrowid; con.close(); return jsonify(ok=True,id=mid)

@app.get("/api/messages/<int:chat_id>")
@login_required
def messages(chat_id):
    con=db(); member=con.execute("SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?",(chat_id,session["user_id"])).fetchone()
    if not member: con.close(); return jsonify(error="Not a chat member"),403
    rows=con.execute("SELECT messages.id,messages.body,messages.message_type,messages.file_name,messages.file_url,messages.created_at,users.id user_id,users.name FROM messages JOIN users ON users.id=messages.user_id WHERE chat_id=? ORDER BY messages.id",(chat_id,)).fetchall(); con.close(); return jsonify([dict(r) for r in rows])

@app.get("/api/users")
@login_required
def users_api():
    con=db(); rows=con.execute("SELECT id,name,phone,org_name,org_type FROM users WHERE id<>? ORDER BY name",(session["user_id"],)).fetchall(); con.close(); return jsonify([dict(r) for r in rows])

@app.post("/api/upload")
@login_required
def upload():
    f=request.files.get("file")
    if not f or not f.filename: return jsonify(error="No file selected"),400
    raw=f.read()
    if len(raw)>20*1024*1024: return jsonify(error="File must be 20 MB or smaller"),400
    mime=f.mimetype or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
    allowed={"application/pdf","text/plain","application/zip","application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    if mime not in allowed and not mime.startswith("image/") and not mime.startswith("audio/") and not mime.startswith("video/"): return jsonify(error="This file type is not supported"),400
    ext=os.path.splitext(f.filename)[1].lower()[:12]; stored=f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(UPLOAD_DIR,stored),"wb") as out: out.write(raw)
    kind="image" if mime.startswith("image/") else "voice" if mime.startswith("audio/") else "video" if mime.startswith("video/") else "document"
    return jsonify(ok=True,file_name=os.path.basename(f.filename),file_url=url_for("uploaded_file",name=stored),message_type=kind)

@app.get("/uploads/<path:name>")
def uploaded_file(name): return send_from_directory(UPLOAD_DIR,name,as_attachment=False)

@app.get("/api/notifications")
@login_required
def notifications():
    con=db(); rows=con.execute("SELECT id,title,body,is_read,created_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50",(session["user_id"],)).fetchall(); con.close(); return jsonify([dict(r) for r in rows])

@app.post("/api/notifications/read")
@login_required
def notifications_read():
    con=db(); con.execute("UPDATE notifications SET is_read=1 WHERE user_id=?",(session["user_id"],)); con.commit(); con.close(); return jsonify(ok=True)

@app.get("/link")
def link_page():
    token=secrets.token_urlsafe(32)
    now=int(time.time())
    expires=now + 300
    con=db()
    con.execute("INSERT INTO linked_devices(user_id,token,device_name,confirmed,created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?)",(0,token,"Computer",0,now,now,expires))
    con.commit(); con.close()
    link_url=urljoin(request.host_url,url_for("link_confirm",token=token))
    return render_template("qr.html",link_url=link_url,token=token,logged_in=bool(session.get("user_id")))

@app.route("/link/confirm")
def link_confirm():
    token=request.args.get("token","")
    con=db()
    row=con.execute("SELECT * FROM linked_devices WHERE token=?",(token,)).fetchone()
    if not row or (row["expires_at"] and row["expires_at"]<int(time.time())):
        con.close(); return render_template("link_confirm.html",token=token,error="This QR code is invalid or expired.")
    if "user_id" not in session:
        con.close(); return render_template("link_confirm.html",token=token,needs_login=True)
    con.execute("UPDATE linked_devices SET user_id=?,confirmed=1,last_seen=? WHERE id=?",(session["user_id"],int(time.time()),row["id"]))
    con.commit(); con.close()
    session.permanent=True
    return render_template("link_confirm.html",success=True)

@app.get("/api/link-status/<token>")
def link_status(token):
    con=db()
    row=con.execute("SELECT confirmed,user_id,last_seen,expires_at FROM linked_devices WHERE token=?",(token,)).fetchone()
    if not row:
        con.close(); return jsonify(error="Link session not found"),404
    if row["expires_at"] and row["expires_at"]<int(time.time()):
        con.close(); return jsonify(error="Link QR expired"),410
    if row["confirmed"] and row["user_id"]>0:
        session["user_id"]=row["user_id"]
        session.permanent=True
        con.execute("UPDATE linked_devices SET last_seen=? WHERE token=?",(int(time.time()),token))
        con.commit(); con.close()
        return jsonify(confirmed=True,login=True,last_seen=int(time.time()))
    con.close()
    return jsonify(confirmed=False,login=False,last_seen=row["last_seen"])

@app.get("/qr.png")
def qr_png():
    data = request.args.get("data", "")
    if not data:
        return ("Missing QR data", 400)
    img = qrcode.make(data)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)

@app.get("/manifest.webmanifest")
def manifest():
    return jsonify(name="TMD Chat",short_name="TMD Chat",description="Private company, shop and team communication",start_url="/home",scope="/",display="standalone",theme_color="#1288e8",background_color="#f4f9ff",icons=[{"src":"/static/icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}])

@app.get("/sw.js")
def service_worker():
    return 'self.addEventListener("install",event=>{self.skipWaiting()});self.addEventListener("activate",event=>{event.waitUntil(self.clients.claim())});self.addEventListener("fetch",event=>{});',200,{"Content-Type":"application/javascript","Cache-Control":"no-cache"}

@app.get("/health")
def health(): return jsonify(ok=True,service="tmd-chat",time=int(time.time()))

init_db()
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=os.environ.get("FLASK_DEBUG")=="1")
