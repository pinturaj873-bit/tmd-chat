import os, sqlite3, secrets, time, mimetypes, uuid
from functools import wraps
from flask import Flask, request, session, redirect, url_for, render_template, jsonify, send_from_directory

APP_DIR=os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR=os.path.join(APP_DIR,'uploads'); os.makedirs(UPLOAD_DIR,exist_ok=True)
DB=os.path.join(APP_DIR,'tmd_chat.db')
app=Flask(__name__); app.secret_key=os.environ.get('SECRET_KEY','change-this-in-production')

def db():
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row; return con

def init_db():
    con=db()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,phone TEXT UNIQUE NOT NULL,name TEXT NOT NULL,avatar TEXT DEFAULT '',role TEXT DEFAULT 'member',org_type TEXT DEFAULT 'company',org_name TEXT DEFAULT '',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS otp_codes(phone TEXT PRIMARY KEY,code TEXT NOT NULL,expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'group',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chat_members(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,joined_at INTEGER NOT NULL,PRIMARY KEY(chat_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,body TEXT NOT NULL,message_type TEXT NOT NULL DEFAULT 'text',file_name TEXT DEFAULT '',file_url TEXT DEFAULT '',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,is_read INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS linked_devices(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,token TEXT UNIQUE NOT NULL,created_at INTEGER NOT NULL,last_seen INTEGER NOT NULL);
    ''')
    if con.execute('SELECT COUNT(*) FROM chats').fetchone()[0]==0:
        now=int(time.time()); con.execute("INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)",('Team Discussion','group',now)); con.execute("INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)",('Company Announcements','announcement',now))
    con.commit(); con.close()

def login_required(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        if 'user_id' not in session:return redirect(url_for('login'))
        return fn(*a,**kw)
    return wrapper

@app.route('/')
def index(): return redirect(url_for('home') if 'user_id' in session else url_for('login'))

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        phone=request.form.get('phone','').strip().replace(' ',''); phone=phone[1:] if phone.startswith('+') else phone
        if not phone.isdigit() or len(phone)<10:return render_template('login.html',error='Please enter a valid mobile number.')
        code=os.environ.get('DEV_OTP') or f'{secrets.randbelow(1000000):06d}'
        con=db(); con.execute('REPLACE INTO otp_codes(phone,code,expires_at) VALUES(?,?,?)',(phone,code,int(time.time())+300)); con.commit(); con.close()
        session['pending_phone']=phone; return render_template('otp.html',dev_otp=code)
    return render_template('login.html')

@app.route('/verify',methods=['POST'])
def verify():
    phone=session.get('pending_phone'); code=request.form.get('code','').strip()
    if not phone:return redirect(url_for('login'))
    con=db(); row=con.execute('SELECT * FROM otp_codes WHERE phone=?',(phone,)).fetchone()
    if not row or row['expires_at']<int(time.time()) or row['code']!=code:
        con.close(); return render_template('otp.html',error='Invalid or expired OTP.')
    user=con.execute('SELECT * FROM users WHERE phone=?',(phone,)).fetchone()
    if user:
        session['user_id']=user['id']; session.pop('pending_phone',None); con.close(); return redirect(url_for('home'))
    session['verified_phone']=phone; session.pop('pending_phone',None); con.close(); return redirect(url_for('profile_setup'))

@app.route('/profile-setup',methods=['GET','POST'])
def profile_setup():
    phone=session.get('verified_phone')
    if not phone:return redirect(url_for('login'))
    if request.method=='POST':
        name=request.form.get('name','').strip(); org_type=request.form.get('org_type','company'); org_name=request.form.get('org_name','').strip()
        if not name or not org_name:return render_template('setup.html',error='Name and company/shop/team are required.')
        con=db(); cur=con.execute('INSERT INTO users(phone,name,role,org_type,org_name,created_at) VALUES(?,?,?,?,?,?)',(phone,name,'member',org_type,org_name,int(time.time()))); con.commit(); uid=cur.lastrowid; con.close()
        session['user_id']=uid; session.pop('verified_phone',None); return redirect(url_for('home'))
    return render_template('setup.html',phone=phone)

@app.route('/home')
@login_required
def home():
    con=db(); user=con.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    chats=con.execute("SELECT c.*,COALESCE((SELECT body FROM messages m WHERE m.chat_id=c.id ORDER BY m.id DESC LIMIT 1),'No messages yet') last_message FROM chats c ORDER BY c.id DESC").fetchall()
    users=con.execute('SELECT id,name,phone,org_name,org_type FROM users WHERE id<>? ORDER BY name',(session['user_id'],)).fetchall(); con.close()
    return render_template('home.html',user=user,chats=chats,users=users)

@app.route('/chat/<int:chat_id>')
@login_required
def chat(chat_id):
    con=db(); user=con.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone(); chat=con.execute('SELECT * FROM chats WHERE id=?',(chat_id,)).fetchone()
    member=con.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?',(chat_id,session['user_id'])).fetchone()
    if not chat or not member:con.close(); return redirect(url_for('home'))
    msgs=con.execute('SELECT messages.*,users.name FROM messages JOIN users ON users.id=messages.user_id WHERE chat_id=? ORDER BY messages.id',(chat_id,)).fetchall()
    members=con.execute('SELECT users.id,users.name,users.phone,users.org_name FROM chat_members JOIN users ON users.id=chat_members.user_id WHERE chat_members.chat_id=? ORDER BY users.name',(chat_id,)).fetchall()
    con.close(); return render_template('chat.html',user=user,chat=chat,messages=msgs,members=members)

@app.post('/api/direct-chat')
@login_required
def direct_chat():
    data=request.get_json() or {}; other_id=data.get('user_id'); con=db(); other=con.execute('SELECT * FROM users WHERE id=?',(other_id,)).fetchone()
    if not other or other['id']==session['user_id']:con.close(); return jsonify(error='User not found'),404
    existing=con.execute("SELECT c.id FROM chats c JOIN chat_members a ON a.chat_id=c.id JOIN chat_members b ON b.chat_id=c.id WHERE c.kind='direct' AND a.user_id=? AND b.user_id=? GROUP BY c.id HAVING COUNT(*)=2",(session['user_id'],other_id)).fetchone()
    if existing:con.close(); return jsonify(ok=True,chat_id=existing['id'],redirect=url_for('chat',chat_id=existing['id']))
    cur=con.execute('INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)',(other['name'],'direct',int(time.time()))); chat_id=cur.lastrowid; now=int(time.time())
    con.executemany('INSERT INTO chat_members VALUES(?,?,?)',[(chat_id,session['user_id'],now),(chat_id,other_id,now)]); con.commit(); con.close()
    return jsonify(ok=True,chat_id=chat_id,redirect=url_for('chat',chat_id=chat_id))

@app.post('/api/chats')
@login_required
def create_chat():
    data=request.get_json() or {}; title=(data.get('title') or '').strip(); kind=data.get('kind','group')
    if not title:return jsonify(error='Chat name is required'),400
    if kind not in ('group','announcement','direct'):kind='group'
    con=db(); cur=con.execute('INSERT INTO chats(title,kind,created_at) VALUES(?,?,?)',(title,kind,int(time.time()))); chat_id=cur.lastrowid
    con.execute('INSERT OR IGNORE INTO chat_members VALUES(?,?,?)',(chat_id,session['user_id'],int(time.time()))); con.commit(); con.close()
    return jsonify(ok=True,chat_id=chat_id,redirect=url_for('chat',chat_id=chat_id))

@app.post('/api/chats/<int:chat_id>/members')
@login_required
def add_member(chat_id):
    data=request.get_json() or {}; uid=data.get('user_id'); con=db(); chat=con.execute('SELECT id FROM chats WHERE id=?',(chat_id,)).fetchone(); user=con.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone()
    if not chat or not user:con.close(); return jsonify(error='Chat or user not found'),404
    con.execute('INSERT OR IGNORE INTO chat_members VALUES(?,?,?)',(chat_id,uid,int(time.time()))); con.commit(); con.close(); return jsonify(ok=True)

@app.post('/api/messages')
@login_required
def send_message():
    data=request.get_json() or {}; body=(data.get('body') or '').strip(); chat_id=data.get('chat_id'); message_type=data.get('message_type','text'); file_name=(data.get('file_name') or '').strip(); file_url=(data.get('file_url') or '').strip()
    if message_type not in ('text','image','document','voice'):message_type='text'
    if not body and not file_url:return jsonify(error='Message or attachment is required'),400
    if not chat_id:return jsonify(error='Chat is required'),400
    con=db(); member=con.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?',(chat_id,session['user_id'])).fetchone()
    if not member:con.close(); return jsonify(error='You are not a member of this chat'),403
    cur=con.execute('INSERT INTO messages(chat_id,user_id,body,message_type,file_name,file_url,created_at) VALUES(?,?,?,?,?,?,?)',(chat_id,session['user_id'],body,message_type,file_name,file_url,int(time.time())))
    others=con.execute('SELECT user_id FROM chat_members WHERE chat_id=? AND user_id<>?',(chat_id,session['user_id'])).fetchall()
    for r in others:con.execute('INSERT INTO notifications(user_id,title,body,is_read,created_at) VALUES(?,?,?,?,?)',(r['user_id'],'New message',body[:120],0,int(time.time())))
    con.commit(); mid=cur.lastrowid; con.close(); return jsonify(ok=True,id=mid)

@app.get('/api/messages/<int:chat_id>')
@login_required
def messages(chat_id):
    con=db(); member=con.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?',(chat_id,session['user_id'])).fetchone()
    if not member:con.close(); return jsonify(error='Not a chat member'),403
    rows=con.execute('SELECT messages.id,messages.body,messages.message_type,messages.file_name,messages.file_url,messages.created_at,users.id user_id,users.name FROM messages JOIN users ON users.id=messages.user_id WHERE chat_id=? ORDER BY messages.id',(chat_id,)).fetchall(); con.close(); return jsonify([dict(r) for r in rows])

@app.get('/api/users')
@login_required
def users_api():
    con=db(); rows=con.execute('SELECT id,name,phone,org_name,org_type FROM users WHERE id<>? ORDER BY name',(session['user_id'],)).fetchall(); con.close(); return jsonify([dict(r) for r in rows])

@app.post('/api/upload')
@login_required
def upload():
    f=request.files.get('file')
    if not f or not f.filename:return jsonify(error='No file selected'),400
    raw=f.read()
    if len(raw)>10*1024*1024:return jsonify(error='File must be 10 MB or smaller'),400
    mime=f.mimetype or mimetypes.guess_type(f.filename)[0] or 'application/octet-stream'
    allowed={'application/pdf','text/plain','image/jpeg','image/png','image/webp','audio/mpeg','audio/wav','audio/ogg','audio/mp4'}
    if mime not in allowed and not mime.startswith('image/') and not mime.startswith('audio/'):return jsonify(error='This file type is not supported'),400
    ext=os.path.splitext(f.filename)[1].lower()[:10]; stored=f'{uuid.uuid4().hex}{ext}'
    with open(os.path.join(UPLOAD_DIR,stored),'wb') as out:out.write(raw)
    kind='image' if mime.startswith('image/') else 'voice' if mime.startswith('audio/') else 'document'
    return jsonify(ok=True,file_name=os.path.basename(f.filename),file_url=url_for('uploaded_file',name=stored),message_type=kind)

@app.get('/uploads/<path:name>')
def uploaded_file(name):return send_from_directory(UPLOAD_DIR,name,as_attachment=False)

@app.get('/api/notifications')
@login_required
def notifications():
    con=db(); rows=con.execute('SELECT id,title,body,is_read,created_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50',(session['user_id'],)).fetchall(); con.close(); return jsonify([dict(r) for r in rows])

@app.post('/api/notifications/read')
@login_required
def notifications_read():
    con=db(); con.execute('UPDATE notifications SET is_read=1 WHERE user_id=?',(session['user_id'],)); con.commit(); con.close(); return jsonify(ok=True)

@app.get('/manifest.webmanifest')
def manifest():return jsonify(name='TMD Chat',short_name='TMD Chat',start_url='/',display='standalone',theme_color='#159bd7',background_color='#ffffff',icons=[])

@app.get('/sw.js')
def service_worker():return "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>self.clients.claim());self.addEventListener('fetch',e=>{});",200,{'Content-Type':'application/javascript'}

@app.get('/health')
def health():return jsonify(ok=True,service='tmd-chat',time=int(time.time()))

init_db()
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=os.environ.get('FLASK_DEBUG')=='1')
