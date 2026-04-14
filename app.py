"""
List It Everywhere — AI-powered crosslisting platform for resellers
Main Flask application
"""

import os
import sqlite3
import hashlib
import secrets
import time
import logging
import json
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, g)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/lie.db")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# ── AI Model Rotation ─────────────────────────────────────────────────────────
FREE_MODELS = [
    "qwen/qwen3-30b-a3b:free",
    "qwen/qwen3-14b:free",
    "meta-llama/llama-4-scout:free",
    "google/gemma-3-12b-it:free",
    "mistralai/mistral-7b-instruct:free",
]

def call_ai(messages, system_prompt=None, max_retries=3):
    if not OPENROUTER_API_KEY:
        raise Exception("AI service not configured.")
    all_messages = []
    if system_prompt:
        all_messages.append({"role": "system", "content": system_prompt})
    all_messages.extend(messages)
    last_error = "unknown"
    for model in FREE_MODELS:
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                             "Content-Type": "application/json",
                             "HTTP-Referer": "https://listiteverywhere.com",
                             "X-Title": "List It Everywhere"},
                    json={"model": model, "messages": all_messages, "max_tokens": 1500},
                    timeout=30,
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    if text and text.strip():
                        return text.strip(), model
                elif resp.status_code == 429:
                    last_error = "rate_limit"
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                elif resp.status_code == 402:
                    last_error = "no_credits"
                    break
                else:
                    last_error = f"http_{resp.status_code}"
                    break
            except requests.exceptions.Timeout:
                last_error = "timeout"
                time.sleep(2 ** attempt)
            except Exception as e:
                last_error = str(e)
                break
    msgs = {"rate_limit": "AI is busy. Try again in 60 seconds.",
            "no_credits": "AI unavailable. Contact support.",
            "timeout": "AI timed out. Try again."}
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
def close_db(error):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'trial',
            trial_ends_at TEXT,
            ai_credits_used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            status TEXT DEFAULT 'draft',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS platform_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            posted_at TEXT,
            url TEXT,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        );

        CREATE TABLE IF NOT EXISTS rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            hits INTEGER DEFAULT 1,
            window_start TEXT DEFAULT (datetime('now'))
        );
    """)
    # Default admin
    ph = hashlib.sha256(b"admin1").hexdigest()
    db.execute("""INSERT OR IGNORE INTO users (email, password_hash, plan, trial_ends_at)
                  VALUES ('admin@listiteverywhere.com', ?, 'pro', '2099-12-31')""", (ph,))
    db.commit()
    logger.info("Database initialized")

# ── Auth Helpers ──────────────────────────────────────────────────────────────
def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()

def rate_limit(endpoint, max_hits=10, window_seconds=60):
    ip = request.remote_addr
    db = get_db()
    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    row = db.execute(
        "SELECT id, hits, window_start FROM rate_limits WHERE ip=? AND endpoint=? AND window_start > ?",
        (ip, endpoint, cutoff.isoformat())
    ).fetchone()
    if row:
        if row['hits'] >= max_hits:
            return False
        db.execute("UPDATE rate_limits SET hits=hits+1 WHERE id=?", (row['id'],))
    else:
        db.execute("DELETE FROM rate_limits WHERE ip=? AND endpoint=?", (ip, endpoint))
        db.execute("INSERT INTO rate_limits (ip, endpoint) VALUES (?,?)", (ip, endpoint))
    db.commit()
    return True

# ── Routes: Auth ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template("landing.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        if not rate_limit("signup", 5, 300):
            flash("Too many signup attempts. Please wait.", "error")
            return render_template("signup.html")
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        if not email or not password or len(password) < 6:
            flash("Valid email and password (6+ chars) required.", "error")
            return render_template("signup.html")
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("Email already registered.", "error")
            return render_template("signup.html")
        trial_ends = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d")
        db.execute(
            "INSERT INTO users (email, password_hash, plan, trial_ends_at) VALUES (?,?,?,?)",
            (email, hash_password(password), "trial", trial_ends)
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        session['user_id'] = user['id']
        session['user_email'] = user['email']
        flash(f"Welcome! Your 14-day free trial starts now.", "success")
        return redirect(url_for('dashboard'))
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not rate_limit("login", 10, 60):
            flash("Too many login attempts. Wait a minute.", "error")
            return render_template("login.html")
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
                          (email, hash_password(password))).fetchone()
        if not user:
            flash("Invalid email or password.", "error")
            return render_template("login.html")
        session['user_id'] = user['id']
        session['user_email'] = user['email']
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
    db = get_db()
    listings = db.execute(
        "SELECT * FROM listings WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (user['id'],)
    ).fetchall()
    total = db.execute("SELECT COUNT(*) as c FROM listings WHERE user_id=?", (user['id'],)).fetchone()['c']
    posted = db.execute(
        "SELECT COUNT(*) as c FROM platform_posts pp JOIN listings l ON pp.listing_id=l.id WHERE l.user_id=? AND pp.status='posted'",
        (user['id'],)
    ).fetchone()['c']
    return render_template("dashboard.html", user=user, listings=listings,
                           total=total, posted=posted)

# ── Routes: Listings ──────────────────────────────────────────────────────────
@app.route("/listings/new")
@login_required
def new_listing():
    return render_template("listing_form.html", listing=None)

@app.route("/listings/create", methods=["POST"])
@login_required
def create_listing():
    user = get_current_user()
    db = get_db()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    price = request.form.get("price", "").strip()
    condition = request.form.get("condition", "Good")
    category = request.form.get("category", "").strip()
    brand = request.form.get("brand", "").strip()
    size = request.form.get("size", "").strip()
    color = request.form.get("color", "").strip()
    tags = request.form.get("tags", "").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for('new_listing'))
    db.execute("""
        INSERT INTO listings (user_id, title, description, price, condition,
                              category, brand, size, color, tags, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,'draft')
    """, (user['id'], title, description, price, condition, category, brand, size, color, tags))
    db.commit()
    listing_id = db.execute("SELECT last_insert_rowid() as id").fetchone()['id']
    flash("Listing created!", "success")
    return redirect(url_for('edit_listing', listing_id=listing_id))

@app.route("/listings/<int:listing_id>")
@login_required
def view_listing(listing_id):
    user = get_current_user()
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=? AND user_id=?",
                         (listing_id, user['id'])).fetchone()
    if not listing:
        flash("Listing not found.", "error")
        return redirect(url_for('dashboard'))
    posts = db.execute("SELECT * FROM platform_posts WHERE listing_id=?", (listing_id,)).fetchall()
    return render_template("listing_view.html", listing=listing, posts=posts)

@app.route("/listings/<int:listing_id>/edit", methods=["GET", "POST"])
@login_required
def edit_listing(listing_id):
    user = get_current_user()
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=? AND user_id=?",
                         (listing_id, user['id'])).fetchone()
    if not listing:
        flash("Listing not found.", "error")
        return redirect(url_for('dashboard'))
    if request.method == "POST":
        db.execute("""
            UPDATE listings SET title=?, description=?, price=?, condition=?,
            category=?, brand=?, size=?, color=?, tags=?, updated_at=datetime('now')
            WHERE id=?
        """, (request.form.get("title"), request.form.get("description"),
              request.form.get("price"), request.form.get("condition"),
              request.form.get("category"), request.form.get("brand"),
              request.form.get("size"), request.form.get("color"),
              request.form.get("tags"), listing_id))
        db.commit()
        flash("Listing updated!", "success")
        return redirect(url_for('edit_listing', listing_id=listing_id))
    posts = db.execute("SELECT * FROM platform_posts WHERE listing_id=?", (listing_id,)).fetchall()
    platforms = ["eBay", "Poshmark", "Mercari", "Depop", "Etsy", "Facebook Marketplace",
                 "Grailed", "Vinted", "OfferUp", "Craigslist"]
    return render_template("listing_form.html", listing=listing, posts=posts, platforms=platforms)

@app.route("/listings/<int:listing_id>/delete", methods=["POST"])
@login_required
def delete_listing(listing_id):
    user = get_current_user()
    db = get_db()
    db.execute("DELETE FROM platform_posts WHERE listing_id=?", (listing_id,))
    db.execute("DELETE FROM listings WHERE id=? AND user_id=?", (listing_id, user['id']))
    db.commit()
    flash("Listing deleted.", "success")
    return redirect(url_for('dashboard'))

# ── Routes: AI ────────────────────────────────────────────────────────────────
@app.route("/listings/<int:listing_id>/ai-generate", methods=["POST"])
@login_required
def ai_generate(listing_id):
    user = get_current_user()
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=? AND user_id=?",
                         (listing_id, user['id'])).fetchone()
    if not listing:
        return jsonify({"error": "Listing not found"}), 404

    item_details = f"""
Item: {listing['title']}
Category: {listing['category'] or 'General'}
Brand: {listing['brand'] or 'Unknown'}
Condition: {listing['condition']}
Size: {listing['size'] or 'N/A'}
Color: {listing['color'] or 'N/A'}
Price: ${listing['price'] or 'TBD'}
""".strip()

    try:
        result, model = call_ai(
            [{"role": "user", "content": f"Write a reseller listing for this item:\n{item_details}"}],
            system_prompt="""You are an expert reseller copywriter. Write compelling listings that sell fast.
Return JSON with exactly these fields:
{
  "title": "optimized SEO title under 80 chars",
  "description": "2-3 paragraph description, highlight condition and features",
  "tags": "comma-separated keywords for search"
}
Return ONLY valid JSON, no extra text."""
        )
        # Parse JSON response
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            import re
            match = re.search(r'\{.*\}', result, re.DOTALL)
            data = json.loads(match.group()) if match else {}

        if data:
            db.execute("""UPDATE listings SET title=?, description=?, tags=?, updated_at=datetime('now')
                          WHERE id=?""",
                       (data.get('title', listing['title']),
                        data.get('description', listing['description']),
                        data.get('tags', listing['tags']),
                        listing_id))
            db.execute("UPDATE users SET ai_credits_used=ai_credits_used+1 WHERE id=?", (user['id'],))
            db.commit()
            return jsonify({"success": True, "data": data, "model": model})
        else:
            return jsonify({"error": "AI returned invalid format. Try again."}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.route("/listings/<int:listing_id>/export/<platform>")
@login_required
def export_listing(listing_id, platform):
    """Generate platform-specific formatted listing text."""
    user = get_current_user()
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=? AND user_id=?",
                         (listing_id, user['id'])).fetchone()
    if not listing:
        return jsonify({"error": "Not found"}), 404

    templates = {
        "eBay": f"""Title: {listing['title']}
Condition: {listing['condition']}
Brand: {listing['brand'] or 'Unbranded'}
Category: {listing['category'] or 'Other'}
Price: ${listing['price']}

Description:
{listing['description']}

Tags/Keywords: {listing['tags']}""",

        "Poshmark": f"""✨ {listing['title']} ✨

{listing['description']}

Brand: {listing['brand'] or 'Other'}
Size: {listing['size'] or 'See description'}
Condition: {listing['condition']}
Color: {listing['color'] or 'See photos'}

Tags: {listing['tags']}""",

        "Mercari": f"""{listing['title']}

{listing['description']}

Condition: {listing['condition']}
Brand: {listing['brand'] or 'No brand'}
Size: {listing['size'] or 'N/A'}
Price: ${listing['price']}""",

        "Depop": f"""{listing['title']}

{listing['description']}

Size: {listing['size'] or 'See description'}
Colour: {listing['color'] or 'See photos'}
Condition: {listing['condition']}
Brand: {listing['brand'] or 'Other'}

#{' #'.join(listing['tags'].split(',')[:10]) if listing['tags'] else ''}""",

        "Etsy": f"""Title: {listing['title']}

Description:
{listing['description']}

Details:
- Condition: {listing['condition']}
- Brand: {listing['brand'] or 'Handmade/Other'}
- Size: {listing['size'] or 'See description'}
- Color: {listing['color'] or 'See photos'}

Tags (add to Etsy tags field):
{listing['tags']}""",
    }

    text = templates.get(platform, f"{listing['title']}\n\n{listing['description']}")

    # Record the export
    db.execute("""INSERT OR REPLACE INTO platform_posts (listing_id, platform, status, posted_at)
                  VALUES (?, ?, 'exported', datetime('now'))""", (listing_id, platform))
    db.commit()

    return jsonify({"success": True, "platform": platform, "text": text})

# ── Routes: Health ────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    try:
        db = get_db()
        db.execute("SELECT 1")
        return jsonify({"status": "ok", "db": "ok"})
    except Exception:
        return jsonify({"status": "degraded"}), 503

@app.route("/sitemap.xml")
def sitemap():
    return app.response_class("""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://listiteverywhere.com/</loc><priority>1.0</priority></url>
  <url><loc>https://listiteverywhere.com/signup</loc><priority>0.9</priority></url>
  <url><loc>https://listiteverywhere.com/login</loc><priority>0.7</priority></url>
</urlset>""", mimetype="application/xml")

@app.route("/robots.txt")
def robots():
    return app.response_class(
        "User-agent: *\nAllow: /\nDisallow: /dashboard\nDisallow: /listings\nDisallow: /admin\n",
        mimetype="text/plain")

# ── Error Handlers ────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, msg="Page not found"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, msg="Something went wrong"), 500

# ── Init ──────────────────────────────────────────────────────────────────────
with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
