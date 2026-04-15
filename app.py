"""
List It Everywhere — Multi-Tenant AI Crosslisting Platform
Full SaaS with overseer panel, tenant isolation, AI chat, and form builder.
"""

import os, sqlite3, hashlib, secrets, time, logging, json, requests
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, g)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/lie.db")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

FREE_MODELS = [
    "qwen/qwen3-30b-a3b:free",
    "qwen/qwen3-14b:free",
    "meta-llama/llama-4-scout:free",
    "google/gemma-3-12b-it:free",
    "mistralai/mistral-7b-instruct:free",
]

PLANS = {
    "trial":   {"name": "Free Trial", "listings": 50,  "ai_credits": 50,  "price": 0},
    "starter": {"name": "Starter",    "listings": 100, "ai_credits": 100, "price": 19.95},
    "pro":     {"name": "Pro",        "listings": 9999,"ai_credits": 9999,"price": 39.95},
    "teams":   {"name": "Teams",      "listings": 9999,"ai_credits": 9999,"price": 79.95},
}

# ── AI ────────────────────────────────────────────────────────────────────────
def call_ai(messages, system_prompt=None, max_retries=3):
    key = get_openrouter_key()
    if not key:
        raise Exception("AI not configured — add your OpenRouter key in Settings ⚙️")
    all_msgs = []
    if system_prompt:
        all_msgs.append({"role": "system", "content": system_prompt})
    all_msgs.extend(messages)
    last_error = "unknown"
    for model in FREE_MODELS:
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {get_openrouter_key()}",
                             "Content-Type": "application/json",
                             "HTTP-Referer": "https://listiteverywhere.com",
                             "X-Title": "List It Everywhere"},
                    json={"model": model, "messages": all_msgs, "max_tokens": 1500},
                    timeout=30)
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    if text and text.strip():
                        return text.strip(), model
                elif resp.status_code == 429:
                    last_error = "rate_limit"
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                elif resp.status_code == 402:
                    last_error = "no_credits"; break
                else:
                    last_error = f"http_{resp.status_code}"; break
            except requests.exceptions.Timeout:
                last_error = "timeout"; time.sleep(2 ** attempt)
            except Exception as e:
                last_error = str(e); break
    msgs = {"rate_limit": "AI is busy right now. Try again in 60 seconds.",
            "no_credits": "AI service unavailable. Contact support.",
            "timeout": "AI timed out. Please try again."}
    raise Exception(msgs.get(last_error, "AI temporarily unavailable."))

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
    return db

@app.teardown_appcontext
def close_db(e):
    db = getattr(g, '_database', None)
    if db is not None: db.close()

def get_config(key, default=''):
    db = get_db()
    row = db.execute('SELECT value FROM app_config WHERE key=?', (key,)).fetchone()
    return row[0] if row else default

def set_config(key, value):
    db = get_db()
    db.execute('INSERT OR REPLACE INTO app_config (key,value) VALUES (?,?)', (key, value))
    db.commit()

def get_openrouter_key():
    return get_config('openrouter_key') or OPENROUTER_API_KEY

def get_openrouter_model():
    return get_config('openrouter_model', 'google/gemini-flash-1.5')

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            plan TEXT DEFAULT 'trial',
            trial_ends_at TEXT,
            stripe_customer_id TEXT,
            ai_credits_used INTEGER DEFAULT 0,
            listings_count INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'owner',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            price TEXT,
            condition TEXT DEFAULT 'Good',
            category TEXT,
            brand TEXT,
            size TEXT,
            color TEXT,
            sku TEXT,
            photos_json TEXT DEFAULT '[]',
            tags TEXT,
            custom_fields TEXT DEFAULT '{}',
            status TEXT DEFAULT 'draft',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS platform_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            status TEXT DEFAULT 'exported',
            posted_at TEXT DEFAULT (datetime('now')),
            url TEXT,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        );

        CREATE TABLE IF NOT EXISTS custom_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            field_type TEXT DEFAULT 'text',
            field_options TEXT,
            required INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            hits INTEGER DEFAULT 1,
            window_start TEXT DEFAULT (datetime('now'))
        );
    """)
    # Overseer admin account
    ph = hashlib.sha256(b"admin1").hexdigest()
    db.execute("INSERT OR IGNORE INTO users (tenant_id, email, password_hash, role) VALUES (NULL,'admin@listiteverywhere.com',?,'overseer')", (ph,))
    db.commit()
    logger.info("DB initialized")

# ── Auth ──────────────────────────────────────────────────────────────────────
def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def overseer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'overseer':
            flash("Access denied.", "error")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' not in session: return None
    return get_db().execute("SELECT u.*, t.plan, t.trial_ends_at, t.ai_credits_used, t.name as tenant_name, t.active as tenant_active FROM users u LEFT JOIN tenants t ON u.tenant_id=t.id WHERE u.id=?", (session['user_id'],)).fetchone()

def check_trial(user):
    if not user or user['role'] == 'overseer': return True
    if user['plan'] == 'trial':
        trial_end = user['trial_ends_at']
        if trial_end and datetime.utcnow().strftime("%Y-%m-%d") > trial_end:
            return False
    return True

def rate_limit(endpoint, max_hits=10, window_sec=60):
    ip = request.remote_addr
    db = get_db()
    cutoff = (datetime.utcnow() - timedelta(seconds=window_sec)).isoformat()
    row = db.execute("SELECT id,hits FROM rate_limits WHERE ip=? AND endpoint=? AND window_start>?", (ip, endpoint, cutoff)).fetchone()
    if row:
        if row['hits'] >= max_hits: return False
        db.execute("UPDATE rate_limits SET hits=hits+1 WHERE id=?", (row['id'],))
    else:
        db.execute("DELETE FROM rate_limits WHERE ip=? AND endpoint=?", (ip, endpoint))
        db.execute("INSERT INTO rate_limits (ip,endpoint) VALUES (?,?)", (ip, endpoint))
    db.commit()
    return True

# ── Routes: Public ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    return render_template("landing.html")

@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method == "POST":
        if not rate_limit("signup", 5, 300):
            flash("Too many attempts. Wait a few minutes.", "error")
            return render_template("signup.html")
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","").strip()
        biz_name = request.form.get("biz_name","").strip() or email.split("@")[0]
        if not email or not password or len(password) < 6:
            flash("Valid email and password (6+ chars) required.", "error")
            return render_template("signup.html")
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("Email already registered.", "error")
            return render_template("signup.html")
        # Create tenant
        slug = biz_name.lower().replace(" ","_")[:30] + "_" + secrets.token_hex(3)
        trial_end = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d")
        db.execute("INSERT INTO tenants (name,slug,plan,trial_ends_at) VALUES (?,?,?,?)", (biz_name, slug, "trial", trial_end))
        db.commit()
        tenant = db.execute("SELECT id FROM tenants WHERE slug=?", (slug,)).fetchone()
        db.execute("INSERT INTO users (tenant_id,email,password_hash,role) VALUES (?,?,?,?)", (tenant['id'], email, hash_pw(password), "owner"))
        db.commit()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        session['user_id'] = user['id']
        session['tenant_id'] = tenant['id']
        session['role'] = 'owner'
        flash(f"Welcome to List It Everywhere! Your 14-day free trial has started.", "success")
        return redirect(url_for('dashboard'))
    return render_template("signup.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if not rate_limit("login", 10, 60):
            flash("Too many attempts. Wait a minute.", "error")
            return render_template("login.html")
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","").strip()
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?", (email, hash_pw(password))).fetchone()
        if not user:
            flash("Invalid email or password.", "error")
            return render_template("login.html")
        session['user_id']   = user['id']
        session['tenant_id'] = user['tenant_id']
        session['role']      = user['role']
        if user['role'] == 'overseer':
            return redirect(url_for('overseer'))
        return redirect(url_for('dashboard'))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('index'))

# ── Routes: Dashboard ─────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    if not check_trial(user):
        return render_template("trial_expired.html", user=user)
    db = get_db()
    tid = session['tenant_id']
    listings = db.execute("SELECT * FROM listings WHERE tenant_id=? ORDER BY created_at DESC LIMIT 20", (tid,)).fetchall()
    total  = db.execute("SELECT COUNT(*) as c FROM listings WHERE tenant_id=?", (tid,)).fetchone()['c']
    posted = db.execute("SELECT COUNT(*) as c FROM platform_posts pp JOIN listings l ON pp.listing_id=l.id WHERE l.tenant_id=?", (tid,)).fetchone()['c']
    plan   = PLANS.get(user['plan'] or 'trial', PLANS['trial'])
    return render_template("dashboard.html", user=user, listings=listings, total=total, posted=posted, plan=plan)

# ── Routes: Listings ──────────────────────────────────────────────────────────
@app.route("/listings/new")
@login_required
def new_listing():
    user = get_current_user()
    if not check_trial(user): return render_template("trial_expired.html", user=user)
    db = get_db()
    custom_fields = db.execute("SELECT * FROM custom_fields WHERE tenant_id=? ORDER BY sort_order", (session['tenant_id'],)).fetchall()
    return render_template("listing_form.html", listing=None, custom_fields=custom_fields)

@app.route("/listings/create", methods=["POST"])
@login_required
def create_listing():
    user = get_current_user()
    db = get_db()
    tid = session['tenant_id']
    custom_fields = db.execute("SELECT * FROM custom_fields WHERE tenant_id=?", (tid,)).fetchall()
    custom_data = {cf['field_name']: request.form.get(f"custom_{cf['field_name']}","") for cf in custom_fields}
    title = request.form.get("title","").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for('new_listing'))
    db.execute("""INSERT INTO listings (tenant_id,user_id,title,description,price,condition,category,brand,size,color,sku,tags,custom_fields,status)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'draft')""",
               (tid, user['id'], title,
                request.form.get("description",""),
                request.form.get("price",""),
                request.form.get("condition","Good"),
                request.form.get("category",""),
                request.form.get("brand",""),
                request.form.get("size",""),
                request.form.get("color",""),
                request.form.get("sku",""),
                request.form.get("tags",""),
                json.dumps(custom_data)))
    db.commit()
    listing_id = db.execute("SELECT last_insert_rowid() as id").fetchone()['id']
    db.execute("UPDATE tenants SET listings_count=listings_count+1 WHERE id=?", (tid,))
    db.commit()
    flash("Listing created! Use AI to write your description.", "success")
    return redirect(url_for('edit_listing', listing_id=listing_id))

@app.route("/listings/<int:listing_id>/edit", methods=["GET","POST"])
@login_required
def edit_listing(listing_id):
    user = get_current_user()
    db = get_db()
    tid = session['tenant_id']
    listing = db.execute("SELECT * FROM listings WHERE id=? AND tenant_id=?", (listing_id, tid)).fetchone()
    if not listing:
        flash("Listing not found.", "error")
        return redirect(url_for('dashboard'))
    if request.method == "POST":
        custom_fields_def = db.execute("SELECT * FROM custom_fields WHERE tenant_id=?", (tid,)).fetchall()
        custom_data = {cf['field_name']: request.form.get(f"custom_{cf['field_name']}","") for cf in custom_fields_def}
        db.execute("""UPDATE listings SET title=?,description=?,price=?,condition=?,category=?,brand=?,size=?,color=?,sku=?,tags=?,custom_fields=?,updated_at=datetime('now') WHERE id=?""",
                   (request.form.get("title"), request.form.get("description"), request.form.get("price"),
                    request.form.get("condition"), request.form.get("category"), request.form.get("brand"),
                    request.form.get("size"), request.form.get("color"), request.form.get("sku"),
                    request.form.get("tags"), json.dumps(custom_data), listing_id))
        db.commit()
        flash("Saved!", "success")
        return redirect(url_for('edit_listing', listing_id=listing_id))
    custom_fields = db.execute("SELECT * FROM custom_fields WHERE tenant_id=? ORDER BY sort_order", (tid,)).fetchall()
    posts = db.execute("SELECT * FROM platform_posts WHERE listing_id=? ORDER BY posted_at DESC", (listing_id,)).fetchall()
    platforms = ["eBay","Poshmark","Mercari","Depop","Etsy","Facebook Marketplace","Grailed","Vinted","OfferUp","Craigslist"]
    custom_vals = json.loads(listing['custom_fields'] or '{}')
    return render_template("listing_form.html", listing=listing, posts=posts,
                           platforms=platforms, custom_fields=custom_fields, custom_vals=custom_vals)

@app.route("/listings/<int:listing_id>/delete", methods=["POST"])
@login_required
def delete_listing(listing_id):
    db = get_db()
    tid = session['tenant_id']
    db.execute("DELETE FROM platform_posts WHERE listing_id=?", (listing_id,))
    db.execute("DELETE FROM listings WHERE id=? AND tenant_id=?", (listing_id, tid))
    db.commit()
    flash("Listing deleted.", "success")
    return redirect(url_for('dashboard'))

# ── Routes: AI Generate ───────────────────────────────────────────────────────
@app.route("/listings/<int:listing_id>/ai-generate", methods=["POST"])
@login_required
def ai_generate(listing_id):
    user = get_current_user()
    db = get_db()
    tid = session['tenant_id']
    listing = db.execute("SELECT * FROM listings WHERE id=? AND tenant_id=?", (listing_id, tid)).fetchone()
    if not listing: return jsonify({"error": "Not found"}), 404
    prompt = f"""Item: {listing['title']}
Category: {listing['category'] or 'General'}
Brand: {listing['brand'] or 'Unknown'}
Condition: {listing['condition']}
Size: {listing['size'] or 'N/A'}
Color: {listing['color'] or 'N/A'}
Price: ${listing['price'] or 'TBD'}"""
    try:
        result, model = call_ai(
            [{"role":"user","content":f"Write a reseller listing:\n{prompt}"}],
            system_prompt="""You are an expert reseller copywriter who knows eBay, Poshmark, Mercari, and Depop inside out.
Return ONLY valid JSON with these exact fields:
{"title":"SEO-optimized title under 80 chars","description":"2-3 paragraphs, highlight condition and appeal","tags":"comma-separated search keywords"}""")
        try: data = json.loads(result)
        except:
            import re
            m = re.search(r'\{.*\}', result, re.DOTALL)
            data = json.loads(m.group()) if m else {}
        if data:
            db.execute("UPDATE listings SET title=?,description=?,tags=?,updated_at=datetime('now') WHERE id=?",
                       (data.get('title', listing['title']), data.get('description', listing['description']),
                        data.get('tags', listing['tags']), listing_id))
            db.execute("UPDATE tenants SET ai_credits_used=ai_credits_used+1 WHERE id=?", (tid,))
            db.commit()
            return jsonify({"success": True, "data": data, "model": model})
        return jsonify({"error": "AI returned invalid format."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 503

# ── Routes: AI Chat ───────────────────────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
@login_required
def ai_chat():
    if not rate_limit("chat", 30, 60):
        return jsonify({"error": "Too many messages. Wait a moment."}), 429
    data = request.get_json()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "No message provided."}), 400
    # Keep last 10 messages for context
    messages = messages[-10:]
    try:
        reply, model = call_ai(
            messages,
            system_prompt="""You are an expert reselling coach and marketplace strategist named Liz.
You help resellers at List It Everywhere sell more items, faster, for higher prices.

You know everything about:
- eBay, Poshmark, Mercari, Depop, Etsy, Facebook Marketplace, Grailed, Vinted, OfferUp
- Pricing strategies and what sells for what price
- Writing killer titles and descriptions that rank in search
- Photography tips that make items sell 3x faster
- Shipping, packaging, and handling returns
- Growing a resale business from side hustle to full income
- Seasonal trends and what categories are hot right now
- How to source inventory cheaply and flip for profit

Be friendly, direct, and give specific actionable advice. Use bullet points when listing steps.
Keep responses concise — 2-4 paragraphs max unless they ask for more detail.""")
        return jsonify({"reply": reply, "model": model})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

# ── Routes: Image AI ────────────────────────────────────────────────────────────
@app.route("/api/analyze-image", methods=["POST"])
@login_required
def analyze_image():
    """Accept a base64 image and return listing details extracted by AI vision."""
    if not rate_limit("analyze_image", 10, 60):
        return jsonify({"error": "Too many requests. Wait a moment."}), 429
    data = request.get_json()
    image_b64 = data.get("image", "")
    if not image_b64:
        return jsonify({"error": "No image provided."}), 400
    # Strip data URL prefix if present
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    if not get_openrouter_key():
        return jsonify({"error": "AI not configured — add your OpenRouter key in Settings ⚙️"}), 503
    # Vision-capable models in priority order
    vision_models = [
        "google/gemini-flash-1.5",
        "google/gemma-3-27b-it",
        "meta-llama/llama-4-scout",
        "qwen/qwen2.5-vl-72b-instruct:free",
        "anthropic/claude-haiku-4-5",
    ]
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": """You are an expert reseller who can identify items from photos.
Analyze this image and return ONLY valid JSON with these fields:
{
  "title": "concise product title under 80 chars for reselling",
  "brand": "brand name or empty string if unknown",
  "category": "one of: Clothing, Shoes, Electronics, Jewelry, Home & Garden, Toys, Books, Sports, Collectibles, Other",
  "condition": "one of: New, Like New, Excellent, Good, Fair, Poor",
  "color": "primary color(s)",
  "description": "2 paragraph reseller description highlighting key features and appeal",
  "tags": "comma-separated search keywords",
  "estimated_price": "suggested selling price as a number e.g. 24.99",
  "details": "any other notable details: size markings, model numbers, materials, era, etc."
}
Return ONLY the JSON object, nothing else."""},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
        ]
    }]
    last_error = "unknown"
    for model in vision_models:
        for attempt in range(2):
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {get_openrouter_key()}",
                             "Content-Type": "application/json",
                             "HTTP-Referer": "https://listiteverywhere.com",
                             "X-Title": "List It Everywhere"},
                    json={"model": model, "messages": messages, "max_tokens": 1000},
                    timeout=45)
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    # Parse JSON
                    import re
                    if text.startswith("```"): text = re.sub(r'^```[\w]*\n?','',text).rstrip('`').strip()
                    try:
                        result = json.loads(text)
                    except:
                        m = re.search(r'\{.*\}', text, re.DOTALL)
                        result = json.loads(m.group()) if m else {}
                    if result:
                        result['model'] = model
                        return jsonify({"success": True, "data": result})
                elif resp.status_code == 429:
                    last_error = "rate_limit"
                    if attempt == 0: time.sleep(2)
                elif resp.status_code in (400, 422):
                    last_error = f"model_unsupported_{model}"
                    break  # Try next model
                else:
                    last_error = f"http_{resp.status_code}"
                    break
            except requests.exceptions.Timeout:
                last_error = "timeout"; break
            except Exception as e:
                last_error = str(e); break
    return jsonify({"error": f"Could not analyze image. Please try again or fill in details manually."}), 503


# ── Routes: Export ────────────────────────────────────────────────────────────
@app.route("/listings/<int:listing_id>/export/<platform>")
@login_required
def export_listing(listing_id, platform):
    db = get_db()
    tid = session['tenant_id']
    l = db.execute("SELECT * FROM listings WHERE id=? AND tenant_id=?", (listing_id, tid)).fetchone()
    if not l: return jsonify({"error": "Not found"}), 404
    tags_fmt = (" #".join(l['tags'].replace(","," ").split()[:10]) if l['tags'] else "")
    templates = {
        "eBay": f"Title: {l['title']}\nCondition: {l['condition']}\nBrand: {l['brand'] or 'Unbranded'}\nCategory: {l['category'] or 'Other'}\nPrice: ${l['price']}\n\nDescription:\n{l['description']}\n\nKeywords: {l['tags']}",
        "Poshmark": f"✨ {l['title']} ✨\n\n{l['description']}\n\nBrand: {l['brand'] or 'Other'}\nSize: {l['size'] or 'See description'}\nCondition: {l['condition']}\nColor: {l['color'] or 'See photos'}\n\nTags: {l['tags']}",
        "Mercari": f"{l['title']}\n\n{l['description']}\n\nCondition: {l['condition']}\nBrand: {l['brand'] or 'No brand'}\nSize: {l['size'] or 'N/A'}\nPrice: ${l['price']}",
        "Depop": f"{l['title']}\n\n{l['description']}\n\nSize: {l['size'] or 'See description'}\nColour: {l['color'] or 'See photos'}\nCondition: {l['condition']}\nBrand: {l['brand'] or 'Other'}\n\n#{tags_fmt}",
        "Etsy": f"Title: {l['title']}\n\n{l['description']}\n\nDetails:\n• Condition: {l['condition']}\n• Brand: {l['brand'] or 'Handmade/Other'}\n• Size: {l['size'] or 'See description'}\n• Color: {l['color'] or 'See photos'}\n\nTags: {l['tags']}",
        "Facebook Marketplace": f"{l['title']} — ${l['price']}\n\n{l['description']}\n\nCondition: {l['condition']}\nBrand: {l['brand'] or 'N/A'}\nSize: {l['size'] or 'N/A'}",
        "Grailed": f"{l['title']}\n\n{l['description']}\n\nSize: {l['size'] or 'See description'}\nColor: {l['color'] or 'See photo'}\nCondition: {l['condition']}\nBrand: {l['brand'] or 'Other'}\nPrice: ${l['price']}\n\nTags: {l['tags']}",
        "Vinted": f"{l['title']}\n\n{l['description']}\n\nBrand: {l['brand'] or 'Other'}\nSize: {l['size'] or 'See description'}\nColor: {l['color'] or 'See photos'}\nCondition: {l['condition']}",
        "OfferUp": f"{l['title']}\n\nPrice: ${l['price']}\nCondition: {l['condition']}\n\n{l['description']}",
        "Craigslist": f"{l['title']} - ${l['price']}\n\n{l['description']}\n\nCondition: {l['condition']}\nBrand: {l['brand'] or 'N/A'}\n\nNo lowballs. Price is firm.",
    }
    text = templates.get(platform, f"{l['title']}\n\n{l['description']}")
    db.execute("INSERT OR REPLACE INTO platform_posts (listing_id,platform,status,posted_at) VALUES (?,?,'exported',datetime('now'))", (listing_id, platform))
    db.commit()
    return jsonify({"success": True, "platform": platform, "text": text})

# ── Routes: Form Builder ──────────────────────────────────────────────────────
@app.route("/settings/fields")
@login_required
def field_builder():
    user = get_current_user()
    db = get_db()
    fields = db.execute("SELECT * FROM custom_fields WHERE tenant_id=? ORDER BY sort_order", (session['tenant_id'],)).fetchall()
    return render_template("field_builder.html", user=user, fields=fields)

@app.route("/settings/fields/add", methods=["POST"])
@login_required
def add_field():
    db = get_db()
    tid = session['tenant_id']
    name   = request.form.get("field_name","").strip().lower().replace(" ","_")
    ftype  = request.form.get("field_type","text")
    opts   = request.form.get("field_options","")
    req    = 1 if request.form.get("required") else 0
    count  = db.execute("SELECT COUNT(*) as c FROM custom_fields WHERE tenant_id=?", (tid,)).fetchone()['c']
    if name:
        db.execute("INSERT INTO custom_fields (tenant_id,field_name,field_type,field_options,required,sort_order) VALUES (?,?,?,?,?,?)",
                   (tid, name, ftype, opts, req, count+1))
        db.commit()
        flash(f"Field '{name}' added!", "success")
    return redirect(url_for('field_builder'))

@app.route("/settings/fields/<int:field_id>/delete", methods=["POST"])
@login_required
def delete_field(field_id):
    db = get_db()
    db.execute("DELETE FROM custom_fields WHERE id=? AND tenant_id=?", (field_id, session['tenant_id']))
    db.commit()
    flash("Field removed.", "success")
    return redirect(url_for('field_builder'))

# ── Routes: Overseer Panel ────────────────────────────────────────────────────
@app.route("/overseer")
@login_required
@overseer_required
def overseer():
    db = get_db()
    tenants = db.execute("""
        SELECT t.*, COUNT(l.id) as listing_count,
               (SELECT COUNT(*) FROM users WHERE tenant_id=t.id) as user_count
        FROM tenants t
        LEFT JOIN listings l ON l.tenant_id=t.id
        GROUP BY t.id ORDER BY t.created_at DESC
    """).fetchall()
    stats = {
        "total_tenants": len(tenants),
        "trial": sum(1 for t in tenants if t['plan'] == 'trial'),
        "paid": sum(1 for t in tenants if t['plan'] not in ('trial',)),
        "total_listings": sum(t['listing_count'] for t in tenants),
        "mrr": sum(PLANS.get(t['plan'], {}).get('price', 0) for t in tenants if t['plan'] not in ('trial',)),
    }
    return render_template("overseer.html", tenants=tenants, stats=stats, plans=PLANS)

@app.route("/overseer/impersonate/<int:tenant_id>")
@login_required
@overseer_required
def impersonate(tenant_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE tenant_id=? AND role='owner'", (tenant_id,)).fetchone()
    if not user:
        flash("No owner found for this tenant.", "error")
        return redirect(url_for('overseer'))
    session['user_id']   = user['id']
    session['tenant_id'] = tenant_id
    session['role']      = 'owner'
    session['impersonating'] = True
    flash(f"Now viewing as tenant #{tenant_id}. Close tab or logout to return.", "success")
    return redirect(url_for('dashboard'))

@app.route("/overseer/tenant/<int:tid>/toggle", methods=["POST"])
@login_required
@overseer_required
def toggle_tenant(tid):
    db = get_db()
    t = db.execute("SELECT active FROM tenants WHERE id=?", (tid,)).fetchone()
    if t:
        db.execute("UPDATE tenants SET active=? WHERE id=?", (0 if t['active'] else 1, tid))
        db.commit()
    return redirect(url_for('overseer'))

@app.route("/overseer/tenant/<int:tid>/plan", methods=["POST"])
@login_required
@overseer_required
def change_plan(tid):
    plan = request.form.get("plan","trial")
    db = get_db()
    db.execute("UPDATE tenants SET plan=? WHERE id=?", (plan, tid))
    db.commit()
    flash(f"Plan updated to {plan}.", "success")
    return redirect(url_for('overseer'))

# ── Routes: Utility ───────────────────────────────────────────────────────────
@app.route("/health")
def health():
    try:
        get_db().execute("SELECT 1")
        return jsonify({"status":"ok","db":"ok"})
    except: return jsonify({"status":"degraded"}), 503

@app.route("/sitemap.xml")
def sitemap():
    return app.response_class('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://listiteverywhere.com/</loc><priority>1.0</priority></url><url><loc>https://listiteverywhere.com/signup</loc><priority>0.9</priority></url></urlset>', mimetype="application/xml")

# ── Admin Settings ────────────────────────────────────────────────────────────
@app.route("/settings/ai", methods=["GET", "POST"])
@overseer_required
def ai_settings():
    if request.method == "POST":
        key = request.form.get("openrouter_key", "").strip()
        model = request.form.get("openrouter_model", "").strip()
        if key:
            set_config("openrouter_key", key)
        if model:
            set_config("openrouter_model", model)
        flash("AI settings saved!", "success")
        return redirect(url_for("ai_settings"))
    return render_template("ai_settings.html",
        current_key=get_openrouter_key(),
        current_model=get_openrouter_model(),
        key_set=bool(get_openrouter_key()))


@app.route("/robots.txt")
def robots():
    return app.response_class("User-agent: *\nAllow: /\nDisallow: /dashboard\nDisallow: /listings\nDisallow: /overseer\n", mimetype="text/plain")

@app.errorhandler(404)
def not_found(e): return render_template("error.html", code=404, msg="Page not found"), 404
@app.errorhandler(500)
def server_error(e): return render_template("error.html", code=500, msg="Something went wrong"), 500

with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
