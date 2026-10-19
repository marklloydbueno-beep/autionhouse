import os
import sqlite3
import re
import secrets
import shutil
import smtplib
from email.message import EmailMessage
from datetime import timedelta
from werkzeug.utils import secure_filename
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, flash, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("DATABASE_PATH", str(BASE_DIR / "goldenhammer.db")))
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
socketio = SocketIO(app, cors_allowed_origins=None, async_mode="threading")

CATEGORIES = ["Fine Art", "Luxury", "Antiques", "Jewelry", "Collectibles", "Furniture", "Electronics"]
# Email verification is optional. It is OFF by default so local users can register and log in
# without SMTP setup. Set REQUIRE_EMAIL_VERIFICATION=1 only after configuring SMTP.
REQUIRE_EMAIL_VERIFICATION = os.environ.get("REQUIRE_EMAIL_VERIFICATION", "0") == "1"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS site_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS auctions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        image_url TEXT NOT NULL DEFAULT '',
        starting_price REAL NOT NULL CHECK(starting_price >= 0),
        current_bid REAL NOT NULL CHECK(current_bid >= 0),
        ends_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_by INTEGER REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS bids (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        auction_id INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id),
        amount REAL NOT NULL CHECK(amount > 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS favorites (user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, auction_id INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, auction_id));
    CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, message TEXT NOT NULL, url TEXT NOT NULL DEFAULT '/', is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL REFERENCES users(id), auction_id INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE, reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS auth_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, token_hash TEXT NOT NULL, purpose TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, auction_id INTEGER NOT NULL REFERENCES auctions(id), buyer_id INTEGER NOT NULL REFERENCES users(id), seller_id INTEGER NOT NULL REFERENCES users(id), amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', payment_reference TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS disputes (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS seller_profiles (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, bio TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, success INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS direct_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        product_url TEXT NOT NULL DEFAULT '',
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK(sender_id <> recipient_id)
    );
    CREATE INDEX IF NOT EXISTS idx_messages_pair ON direct_messages(sender_id, recipient_id, id);
    CREATE TABLE IF NOT EXISTS user_blocks (
        blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(blocker_id, blocked_id), CHECK(blocker_id <> blocked_id)
    );
    CREATE TABLE IF NOT EXISTS user_follows (
        follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        followed_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(follower_id, followed_id), CHECK(follower_id <> followed_id)
    );
    CREATE TABLE IF NOT EXISTS auction_chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        auction_id INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 1000),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_auction_chat ON auction_chat_messages(auction_id,id);
    CREATE TABLE IF NOT EXISTS user_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER NOT NULL REFERENCES users(id),
        reported_user_id INTEGER NOT NULL REFERENCES users(id),
        reason TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Forward-compatible migrations for existing databases.
    columns = {r[1] for r in db.execute("PRAGMA table_info(auctions)")}
    for name, declaration in [("brand", "TEXT NOT NULL DEFAULT ''"), ("condition_label", "TEXT NOT NULL DEFAULT 'good'"), ("age_years", "INTEGER NOT NULL DEFAULT 0"), ("min_increment", "REAL NOT NULL DEFAULT 1"), ("image_filename", "TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:
            db.execute(f"ALTER TABLE auctions ADD COLUMN {name} {declaration}")
    for name, declaration in [("auction_mode", "TEXT NOT NULL DEFAULT 'scheduled'"), ("host_live", "INTEGER NOT NULL DEFAULT 0"), ("host_started_at", "TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:
            db.execute(f"ALTER TABLE auctions ADD COLUMN {name} {declaration}")
    usercols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
    if "email_verified" not in usercols:
        db.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        # Preserve access for accounts that existed before email verification was introduced.
        db.execute("UPDATE users SET email_verified=1")
    defaults = {
        "site_name": "GoldenHammer", "tagline": "AUCTION HOUSE", "theme": "classic",
        "gold_color": "#efbd59", "background_color": "#07101a",
        "announcement_enabled": "1", "announcement_text": "Welcome to GoldenHammer Auction House!",
        "featured_enabled": "1", "maintenance_mode": "0"
    }
    for key, value in defaults.items():
        db.execute("INSERT OR IGNORE INTO site_settings(setting_key,setting_value) VALUES (?,?)", (key, value))
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@goldenhammer.local").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "ChangeMe123!")
    if not db.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone():
        db.execute("INSERT INTO users (name,email,password_hash,role) VALUES (?,?,?,'admin')",
                   ("GoldenHammer Admin", admin_email, generate_password_hash(admin_password)))
    if db.execute("SELECT COUNT(*) FROM auctions").fetchone()[0] == 0:
        demo = [
            ("Coastal Village Scene", "Fine Art", "A framed coastal landscape offered as a sample listing.", "https://images.unsplash.com/photo-1579783902614-a3fb3927b6a5?auto=format&fit=crop&w=900&q=85", 250, "2099-12-31T23:59"),
            ("Classic Diver Watch", "Luxury", "A vintage-inspired diver watch. Sample listing only.", "https://images.unsplash.com/photo-1524805444758-089113d48a6d?auto=format&fit=crop&w=900&q=85", 500, "2099-12-31T23:59"),
            ("Imperial Blue Vase", "Antiques", "Decorative blue porcelain vase, sample listing.", "https://images.unsplash.com/photo-1578749556568-bc2c40e68b61?auto=format&fit=crop&w=900&q=85", 120, "2099-12-31T23:59"),
            ("Diamond Style Ring", "Jewelry", "Jewelry listing demo. No authenticity is implied.", "https://images.unsplash.com/photo-1605100804763-247f67b3557e?auto=format&fit=crop&w=900&q=85", 300, "2099-12-31T23:59"),
        ]
        db.executemany("""INSERT INTO auctions (title,category,description,image_url,starting_price,current_bid,ends_at)
                          VALUES (?,?,?,?,?,?,?)""", [(t,c,d,i,p,p,e) for t,c,d,i,p,e in demo])
    db.commit()
    db.close()


def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute("SELECT id,name,email,role FROM users WHERE id=?", (session["user_id"],)).fetchone()


@app.context_processor
def inject_globals():
    user = current_user()
    unread_messages = 0
    if user and user["role"] == "user":
        unread_messages = get_db().execute("SELECT COUNT(*) FROM direct_messages WHERE recipient_id=? AND is_read=0", (user["id"],)).fetchone()[0]
    rows = get_db().execute("SELECT setting_key,setting_value FROM site_settings").fetchall()
    settings = {row["setting_key"]: row["setting_value"] for row in rows}
    settings.setdefault("site_name", "GoldenHammer")
    settings.setdefault("tagline", "AUCTION HOUSE")
    settings.setdefault("theme", "classic")
    settings.setdefault("gold_color", "#efbd59")
    settings.setdefault("background_color", "#07101a")
    settings.setdefault("announcement_enabled", "1")
    settings.setdefault("announcement_text", "Welcome to GoldenHammer Auction House!")
    settings.setdefault("featured_enabled", "1")
    settings.setdefault("maintenance_mode", "0")
    return {"current_user": user, "categories": CATEGORIES, "unread_messages": unread_messages, "site_settings": settings}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            flash("Please log in as an administrator.", "warning")
            return redirect(url_for("login"))
        if user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    import secrets
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_post_requests():
    if request.method == "POST":
        expected = session.get("_csrf_token")
        supplied = request.form.get("_csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not __import__("hmac").compare_digest(expected, supplied):
            abort(400, description="Invalid or missing CSRF token. Refresh the page and try again.")


@app.route("/")
def index():
    db = get_db()
    category = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    min_price = request.args.get("min_price", "").strip()
    max_price = request.args.get("max_price", "").strip()
    sql = "SELECT * FROM auctions WHERE status='open'"
    params = []
    if category in CATEGORIES:
        sql += " AND category=?"
        params.append(category)
    if search:
        sql += " AND (title LIKE ? OR description LIKE ? OR category LIKE ? OR brand LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like, like])
    try:
        if min_price: sql += " AND current_bid >= ?"; params.append(max(0, float(min_price)))
        if max_price: sql += " AND current_bid <= ?"; params.append(max(0, float(max_price)))
    except ValueError:
        flash("Price filters must be valid numbers.", "warning")
        min_price = max_price = ""
    sql += " ORDER BY id DESC"
    auctions = db.execute(sql, params).fetchall()
    return render_template("index.html", auctions=auctions, selected_category=category, search=search, min_price=min_price, max_price=max_price)


@app.get("/api/search-suggestions")
def search_suggestions():
    """Return quick autocomplete suggestions from GoldenHammer's live listings.

    This is database-backed autocomplete, not a search of the wider internet.
    """
    query = request.args.get("q", "").strip()[:100]
    if len(query) < 1:
        return jsonify({"suggestions": []})

    db = get_db()
    like = f"%{query}%"
    rows = db.execute(
        "SELECT id, title, category, brand FROM auctions "
        "WHERE status='open' AND (title LIKE ? OR category LIKE ? OR brand LIKE ? OR description LIKE ?) "
        "ORDER BY CASE WHEN title LIKE ? THEN 0 WHEN brand LIKE ? THEN 1 ELSE 2 END, id DESC LIMIT 8",
        (like, like, like, like, f"{query}%", f"{query}%"),
    ).fetchall()

    suggestions = []
    seen = set()
    for row in rows:
        title = (row["title"] or "").strip()
        if title and title.casefold() not in seen:
            seen.add(title.casefold())
            suggestions.append({
                "type": "listing",
                "label": title,
                "detail": " · ".join(part for part in [row["brand"], row["category"]] if part),
                "url": url_for("auction_detail", auction_id=row["id"]),
            })

    # Include matching category suggestions so users can search by item type too.
    for category_name in CATEGORIES:
        if query.casefold() in category_name.casefold() and category_name.casefold() not in seen and len(suggestions) < 8:
            seen.add(category_name.casefold())
            suggestions.append({"type": "category", "label": category_name, "detail": "Search this category", "url": url_for("index", category=category_name)})

    if not suggestions:
        suggestions.append({"type": "search", "label": f'Search for “{query}”', "detail": "Show matching listings", "url": url_for("index", q=query)})
    return jsonify({"suggestions": suggestions[:8]})


@app.route("/auction/<int:auction_id>", methods=["GET", "POST"])
def auction_detail(auction_id):
    db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
    if auction is None:
        abort(404)
    if request.method == "POST":
        user = current_user()
        if user is None:
            flash("Log in before placing a bid.", "warning")
            return redirect(url_for("login"))
        if auction["status"] != "open" or _is_expired(auction["ends_at"]):
            db = get_db(); db.execute("UPDATE auctions SET status='closed' WHERE id=?", (auction_id,)); db.commit()
            flash("This auction is closed.", "warning")
            return redirect(url_for("auction_detail", auction_id=auction_id))
        if auction["created_by"] == user["id"]:
            flash("You cannot bid on your own product.", "danger")
            return redirect(url_for("auction_detail", auction_id=auction_id))
        try:
            amount = round(float(request.form.get("amount", "")), 2)
        except (ValueError, TypeError):
            amount = 0
        minimum = round(float(auction["current_bid"]) + float(auction["min_increment"] if "min_increment" in auction.keys() else 1), 2)
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
        if latest["status"] != "open" or amount < round(float(latest["current_bid"]) + float(latest["min_increment"] if "min_increment" in latest.keys() else 1), 2):
            db.rollback()
            flash(f"Your bid must be at least ${minimum:,.2f}.", "danger")
        else:
            previous = db.execute("SELECT user_id FROM bids WHERE auction_id=? ORDER BY amount DESC,id ASC LIMIT 1", (auction_id,)).fetchone()
            db.execute("INSERT INTO bids (auction_id,user_id,amount) VALUES (?,?,?)", (auction_id,user["id"],amount))
            db.execute("UPDATE auctions SET current_bid=? WHERE id=?", (amount,auction_id))
            if previous and previous["user_id"] != user["id"]:
                _notify(previous["user_id"], f"You were outbid on {latest['title']}.", url_for("auction_detail", auction_id=auction_id))
            db.commit()
            socketio.emit("auction_state", {"auction_id": auction_id, "action": "bid"}, to=f"auction_{auction_id}")
            flash("Bid recorded. No payment was taken.", "success")
        return redirect(url_for("auction_detail", auction_id=auction_id))
    bids = db.execute("""SELECT b.amount,b.created_at,u.name FROM bids b JOIN users u ON u.id=b.user_id
                         WHERE b.auction_id=? ORDER BY b.id DESC""", (auction_id,)).fetchall()
    winner = db.execute("SELECT u.name,b.amount,b.user_id FROM bids b JOIN users u ON u.id=b.user_id WHERE b.auction_id=? ORDER BY b.amount DESC,b.id ASC LIMIT 1", (auction_id,)).fetchone() if auction["status"] == "closed" else None
    return render_template("auction.html", auction=auction, bids=bids, winner=winner)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(name) < 2 or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            flash("Enter your name and a valid email address.", "danger")
            return render_template("auth.html", mode="register")
        if len(password) < 8:
            flash("Your password must contain at least 8 characters.", "danger")
            return render_template("auth.html", mode="register")
        if password != confirm_password:
            flash("The passwords do not match. Please try again.", "danger")
            return render_template("auth.html", mode="register")
        try:
            db = get_db()
            # Registered accounts are stored persistently in goldenhammer.db.
            # Passwords are hashed; plaintext passwords are never saved.
            cur = db.execute("INSERT INTO users (name,email,password_hash,role) VALUES (?,?,?,'user')",
                             (name,email,generate_password_hash(password)))
            db.commit()
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "danger")
            return render_template("auth.html", mode="register")
        session.clear()
        if REQUIRE_EMAIL_VERIFICATION:
            token = _issue_token(cur.lastrowid, "verify", 24 * 60)
            verify_link = url_for("verify_email", token=token, _external=True)
            try:
                _send_email(email, "Verify your GoldenHammer email", f"Welcome to GoldenHammer. Verify your email within 24 hours: {verify_link}")
                flash("Account created. Check your email for the verification link before signing in.", "success")
            except Exception:
                app.logger.exception("Verification email failed")
                flash("Your account was created, but the verification email could not be sent. Contact the site administrator or temporarily disable REQUIRE_EMAIL_VERIFICATION for local testing.", "warning")
        else:
            # Local/demo mode: no email delivery is required. Mark the account as usable immediately.
            db.execute("UPDATE users SET email_verified=1 WHERE id=?", (cur.lastrowid,))
            db.commit()
            flash("Account created successfully. You can now log in with your email and password.", "success")
        return redirect(url_for("login"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        recent = db.execute("SELECT COUNT(*) FROM login_attempts WHERE email=? AND success=0 AND attempted_at >= datetime('now','-15 minutes')", (email,)).fetchone()[0]
        if recent >= 8:
            flash("Too many failed sign-in attempts. Please wait 15 minutes before trying again.", "danger")
            return render_template("auth.html", mode="login"), 429
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            db.execute("INSERT INTO login_attempts(email,success) VALUES(?,0)", (email,)); db.commit()
            flash("Email or password is incorrect.", "danger")
            return render_template("auth.html", mode="login")
        db.execute("INSERT INTO login_attempts(email,success) VALUES(?,1)", (email,)); db.commit()
        if user["role"] == "admin":
            flash("Administrator accounts must use the separate admin sign-in. Hold the GoldenHammer logo for 5 seconds to open it.", "warning")
            return render_template("auth.html", mode="login")
        if REQUIRE_EMAIL_VERIFICATION and not user["email_verified"]:
            flash("Please verify your email using the link sent during registration before signing in.", "warning")
            return render_template("auth.html", mode="login"), 403
        session.clear()
        session["user_id"] = user["id"]
        flash(f"Welcome back, {user['name']}!", "success")
        next_url = request.args.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("admin_dashboard") if user["role"] == "admin" else url_for("index"))
    return render_template("auth.html", mode="login")


@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    user = current_user()
    if user and user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        account = get_db().execute("SELECT * FROM users WHERE email=? AND role='admin'", (email,)).fetchone()
        if account is None or not check_password_hash(account["password_hash"], password):
            flash("Administrator email or password is incorrect.", "danger")
            return render_template("admin_login.html")
        session.clear()
        session["user_id"] = account["id"]
        flash("Administrator signed in.", "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_login.html")


@app.route("/my-listings")
@login_required
def my_listings():
    user = current_user()
    listings = get_db().execute("SELECT * FROM auctions WHERE created_by=? ORDER BY id DESC", (user["id"],)).fetchall()
    return render_template("my_listings.html", listings=listings)


def _save_user_listing(auction=None):
    user = current_user()
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "")
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()
    ends_at = request.form.get("ends_at", "").strip()
    try:
        price = round(float(request.form.get("starting_price", "")), 2)
    except (ValueError, TypeError):
        price = -1
    if (not title or len(title) > 120 or category not in CATEGORIES or not description
            or price < 0 or not ends_at or (image_url and not image_url.startswith(("https://", "http://")))):
        flash("Check the title, category, description, image URL, starting price, and end date.", "danger")
        return render_template("auction_form.html", auction=auction, user_listing=True)
    db = get_db()
    if auction:
        bid_count = db.execute("SELECT COUNT(*) FROM bids WHERE auction_id=?", (auction["id"],)).fetchone()[0]
        if bid_count:
            flash("Auction terms cannot be edited after the first bid. Contact an administrator if a correction is required.", "danger")
            return redirect(url_for("my_listings"))
        top_bid = db.execute("SELECT MAX(amount) FROM bids WHERE auction_id=?", (auction["id"],)).fetchone()[0]
        current_bid = max(price, float(top_bid or 0))
        mode = request.form.get("auction_mode", auction["auction_mode"] if "auction_mode" in auction.keys() else "scheduled")
        if mode not in ("scheduled", "hosted"): mode = "scheduled"
        db.execute("UPDATE auctions SET title=?,category=?,description=?,image_url=?,starting_price=?,current_bid=?,ends_at=?,auction_mode=?,min_increment=? WHERE id=? AND created_by=?",
                   (title, category, description, image_url, price, current_bid, ends_at, mode, max(0.01, float(request.form.get("min_increment", "1") or 1)), auction["id"], user["id"]))
        db.commit()
        flash("Your product listing has been updated.", "success")
    else:
        mode = request.form.get("auction_mode", "scheduled") if request.form.get("auction_mode") in ("scheduled", "hosted") else "scheduled"
        db.execute("INSERT INTO auctions (title,category,description,image_url,starting_price,current_bid,ends_at,created_by,brand,condition_label,age_years,min_increment,auction_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (title, category, description, image_url, price, price, ends_at, user["id"], request.form.get("brand", "")[:100], request.form.get("condition_label", "good"), max(0, min(10000, int(request.form.get("age_years", "0") or 0))), max(1, float(request.form.get("min_increment", "1") or 1)), mode))
        db.commit()
        flash("Your product is now listed on GoldenHammer.", "success")
    return redirect(url_for("my_listings"))


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def user_new_product():
    if current_user()["role"] == "admin":
        return redirect(url_for("new_auction"))
    if request.method == "POST":
        return _save_user_listing()
    return render_template("auction_form.html", auction=None, user_listing=True)


@app.route("/products/<int:auction_id>/edit", methods=["GET", "POST"])
@login_required
def user_edit_product(auction_id):
    user = current_user()
    auction = get_db().execute("SELECT * FROM auctions WHERE id=? AND created_by=?", (auction_id, user["id"])).fetchone()
    if auction is None:
        abort(404)
    if request.method == "POST":
        return _save_user_listing(auction)
    return render_template("auction_form.html", auction=auction, user_listing=True)


@app.post("/products/<int:auction_id>/delete")
@login_required
def user_delete_product(auction_id):
    user = current_user()
    db = get_db()
    auction = db.execute("SELECT id FROM auctions WHERE id=? AND created_by=?", (auction_id, user["id"])).fetchone()
    if auction is None:
        abort(404)
    db.execute("DELETE FROM auctions WHERE id=?", (auction_id,))
    db.commit()
    flash("Your product listing has been deleted.", "success")
    return redirect(url_for("my_listings"))


@app.route("/value-check", methods=["GET", "POST"])
def value_check():
    estimate = None
    if request.method == "POST":
        category = request.form.get("category", "Collectibles")
        condition = request.form.get("condition", "good")
        try:
            asking = max(0, float(request.form.get("asking_price", "0") or 0))
        except (ValueError, TypeError):
            asking = 0
        factors = {"Fine Art": 1.0, "Luxury": 1.0, "Antiques": 1.0, "Jewelry": 1.0, "Collectibles": 1.0, "Furniture": 1.0, "Electronics": 1.0}
        condition_factors = {"mint": 1.25, "excellent": 1.10, "good": 1.0, "fair": 0.70, "poor": 0.40}
        age = max(0, min(1000, int(request.form.get("age_years", "0") or 0)))
        brand = request.form.get("brand", "").strip()[:100]
        category_factor = factors.get(category, 1.0)
        condition_factor = condition_factors.get(condition, 1.0)
        age_factor = max(0.45, 1.0 - min(age, 50) * 0.01) if category == "Electronics" else min(1.5, 1.0 + min(age, 100) * 0.003)
        brand_factor = 1.05 if brand else 1.0
        factor = category_factor * condition_factor * age_factor * brand_factor
        if asking > 0:
            low = asking * factor * 0.75
            high = asking * factor * 1.25
            estimate = {"low": low, "high": high, "basis": f"reference price × condition ({condition_factor:.2f}) × age ({age_factor:.2f}) × brand-info factor ({brand_factor:.2f}). Category is neutral until verified sales data is available.", "confidence": "Low — no verified comparable completed sales are connected."}
        else:
            estimate = {"low": None, "high": None, "basis": "Add a reference or asking price to calculate a rough range."}
    return render_template("value_check.html", estimate=estimate)


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    auctions = db.execute("""SELECT a.*, (SELECT COUNT(*) FROM bids b WHERE b.auction_id=a.id) AS bid_count
                             FROM auctions a ORDER BY a.id DESC""").fetchall()
    stats = {"auctions": db.execute("SELECT COUNT(*) FROM auctions").fetchone()[0],
             "users": db.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0],
             "bids": db.execute("SELECT COUNT(*) FROM bids").fetchone()[0]}
    registered_users = db.execute(
        "SELECT id,name,email,role,created_at FROM users ORDER BY id DESC"
    ).fetchall()
    return render_template("admin.html", auctions=auctions, stats=stats, registered_users=registered_users)


@app.route("/admin/site-settings", methods=["GET", "POST"])
@admin_required
def admin_site_settings():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "publish")
        if action == "restore":
            defaults = {
                "site_name": "GoldenHammer", "tagline": "AUCTION HOUSE", "theme": "classic",
                "gold_color": "#efbd59", "background_color": "#07101a",
                "announcement_enabled": "1", "announcement_text": "Welcome to GoldenHammer Auction House!",
                "featured_enabled": "1", "maintenance_mode": "0"
            }
            db.executemany("INSERT INTO site_settings(setting_key,setting_value) VALUES (?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value", defaults.items())
            db.commit()
            flash("Default website appearance and settings restored for everyone.", "success")
            return redirect(url_for("admin_site_settings"))
        site_name = request.form.get("site_name", "GoldenHammer").strip()[:60] or "GoldenHammer"
        tagline = request.form.get("tagline", "AUCTION HOUSE").strip()[:80] or "AUCTION HOUSE"
        theme = request.form.get("theme", "classic")
        if theme not in {"classic", "midnight", "light"}: theme = "classic"
        import re as _re
        gold = request.form.get("gold_color", "#efbd59")
        bg = request.form.get("background_color", "#07101a")
        if not _re.fullmatch(r"#[0-9a-fA-F]{6}", gold): gold = "#efbd59"
        if not _re.fullmatch(r"#[0-9a-fA-F]{6}", bg): bg = "#07101a"
        values = {
            "site_name": site_name, "tagline": tagline, "theme": theme,
            "gold_color": gold, "background_color": bg,
            "announcement_enabled": "1" if request.form.get("announcement_enabled") == "on" else "0",
            "announcement_text": request.form.get("announcement_text", "").strip()[:240],
            "featured_enabled": "1" if request.form.get("featured_enabled") == "on" else "0",
            "maintenance_mode": "1" if request.form.get("maintenance_mode") == "on" else "0"
        }
        db.executemany("INSERT INTO site_settings(setting_key,setting_value) VALUES (?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value", values.items())
        db.commit()
        flash("Published! These website settings now apply to admin and user pages.", "success")
        return redirect(url_for("admin_site_settings"))
    rows = db.execute("SELECT setting_key,setting_value FROM site_settings").fetchall()
    settings = {row["setting_key"]: row["setting_value"] for row in rows}
    return render_template("site_settings.html", settings=settings)


@app.route("/admin/auctions/new", methods=["GET", "POST"])
@admin_required
def new_auction():
    if request.method == "POST":
        title=request.form.get("title","").strip(); category=request.form.get("category","")
        description=request.form.get("description","").strip(); image_url=request.form.get("image_url","").strip()
        ends_at=request.form.get("ends_at","").strip()
        try: price=round(float(request.form.get("starting_price","")),2)
        except (ValueError,TypeError): price=-1
        if not title or len(title)>120 or category not in CATEGORIES or not description or price<0 or not ends_at:
            flash("Please complete all fields and enter a valid starting price.","danger")
            return render_template("auction_form.html",auction=None)
        db=get_db()
        mode = request.form.get("auction_mode", "scheduled") if request.form.get("auction_mode") in ("scheduled", "hosted") else "scheduled"
        increment = max(0.01, float(request.form.get("min_increment", "1") or 1))
        db.execute("""INSERT INTO auctions (title,category,description,image_url,starting_price,current_bid,ends_at,created_by,auction_mode,min_increment)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""",(title,category,description,image_url,price,price,ends_at,current_user()["id"],mode,increment))
        db.commit(); flash("Auction listing created.","success"); return redirect(url_for("admin_dashboard"))
    return render_template("auction_form.html",auction=None)


@app.route("/admin/auctions/<int:auction_id>/edit", methods=["GET","POST"])
@admin_required
def edit_auction(auction_id):
    db=get_db(); auction=db.execute("SELECT * FROM auctions WHERE id=?",(auction_id,)).fetchone()
    if auction is None: abort(404)
    if request.method=="POST":
        title=request.form.get("title","").strip(); category=request.form.get("category","")
        description=request.form.get("description","").strip(); image_url=request.form.get("image_url","").strip()
        ends_at=request.form.get("ends_at","").strip(); status=request.form.get("status","open")
        try: price=round(float(request.form.get("starting_price","")),2)
        except (ValueError,TypeError): price=-1
        if not title or len(title)>120 or category not in CATEGORIES or not description or price<0 or not ends_at or status not in ("open","closed"):
            flash("Please check the listing fields.","danger"); return render_template("auction_form.html",auction=auction)
        top_bid=db.execute("SELECT MAX(amount) FROM bids WHERE auction_id=?",(auction_id,)).fetchone()[0]
        current_bid=max(price,float(top_bid or 0))
        mode = request.form.get("auction_mode", auction["auction_mode"] if "auction_mode" in auction.keys() else "scheduled")
        if mode not in ("scheduled", "hosted"): mode = "scheduled"
        db.execute("""UPDATE auctions SET title=?,category=?,description=?,image_url=?,starting_price=?,current_bid=?,ends_at=?,status=?,auction_mode=?
                      WHERE id=?""",(title,category,description,image_url,price,current_bid,ends_at,status,mode,auction_id))
        db.commit(); flash("Auction listing updated.","success"); return redirect(url_for("admin_dashboard"))
    return render_template("auction_form.html",auction=auction)


@app.post("/admin/auctions/<int:auction_id>/delete")
@admin_required
def delete_auction(auction_id):
    db=get_db()
    if not db.execute("SELECT id FROM auctions WHERE id=?",(auction_id,)).fetchone(): abort(404)
    db.execute("DELETE FROM auctions WHERE id=?",(auction_id,)); db.commit()
    flash("Auction listing deleted.","success"); return redirect(url_for("admin_dashboard"))


# Lightweight local "AI" search assistant: interprets auction-related intent and recommends
# relevant listings using transparent text matching. No external AI/API key required.
def assistant_answer(message):
    msg = message.strip()
    if not msg:
        return {"answer": "Try asking about watches, antiques, jewelry, artwork, bidding, or how auctions work.", "items": []}
    low = msg.lower()
    db = get_db()
    open_items = db.execute("SELECT * FROM auctions WHERE status='open' ORDER BY current_bid ASC").fetchall()
    stop_words = {"find","search","show","me","some","an","a","the","for","please","i","want","looking","what","is","are","can","you","related","item","items","auction","auctions","under","below","less","than","price","cheap","cheapest","recommend","recommendation"}
    terms = [w for w in re.findall(r"[a-z0-9]+", low) if w not in stop_words and len(w) > 1]
    matches = []
    for item in open_items:
        haystack = f"{item['title']} {item['category']} {item['description']}".lower()
        score = sum(2 if term in item["title"].lower() else 1 for term in terms if term in haystack)
        # Understand a few broad intents even if the exact word isn't in a listing.
        if any(w in low for w in ["cheap","affordable","lowest price","budget"]) and item["current_bid"] <= 500:
            score += 2
        if any(w in low for w in ["luxury","expensive","premium"]) and item["category"] == "Luxury":
            score += 2
        if any(w in low for w in ["art","painting","picture"]) and item["category"] == "Fine Art":
            score += 2
        if any(w in low for w in ["old","historic","vintage","antique"]) and item["category"] == "Antiques":
            score += 2
        if any(w in low for w in ["ring","diamond","necklace","jewelry","jewellery"]) and item["category"] == "Jewelry":
            score += 2
        if score > 0:
            matches.append((score, item))
    matches.sort(key=lambda pair: (-pair[0], pair[1]["current_bid"]))
    chosen = [item for _, item in matches[:4]]
    if chosen:
        answer = f"I matched your search to {len(chosen)} auction listing(s). These are suggestions based on listing titles, descriptions, categories, and current demo bids."
    elif any(word in low for word in ["bid","bidding","how does","how do","auction work"]):
        answer = "To bid, open an auction, log in, and enter an amount at least $1 above the current bid. This demo records bids in the local database, but it does not process payments or automatically close auctions."
    elif any(word in low for word in ["price","value","worth","predict","estimate","forecast"]):
        answer = "I can help you search listings and compare their current demo bids, but I cannot reliably predict an item's real market value from this data alone. Real estimates need verified item details, condition, provenance, and historical completed-sale prices."
    else:
        answer = "I couldn't find a close match in the current listings. Try a category or item keyword such as 'watch', 'art', 'antique', 'ring', 'cheap', or 'luxury'. I only search this auction site's listings; I don't automatically know every item on the internet."
    return {"answer": answer, "items": [
        {"id": i["id"], "title": i["title"], "category": i["category"],
         "current_bid": f"${i['current_bid']:,.2f}", "image_url": i["image_url"],
         "url": url_for("auction_detail", auction_id=i["id"])}
        for i in chosen
    ]}


@app.post("/api/assistant")
def api_assistant():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", ""))[:500]
    return jsonify(assistant_answer(message))



def _is_expired(value):
    try:
        return datetime.fromisoformat(value) <= datetime.now()
    except (TypeError, ValueError):
        return True


def _notify(user_id, message, target_url="/"):
    get_db().execute("INSERT INTO notifications (user_id,message,url) VALUES (?,?,?)", (user_id, message, target_url))


def _send_email(to_address, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        app.logger.warning("Email not sent because SMTP_HOST is not configured. Message intended for %s: %s", to_address, subject)
        return False
    msg = EmailMessage(); msg["Subject"] = subject; msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "noreply@example.com")); msg["To"] = to_address; msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as server:
        server.starttls()
        if os.environ.get("SMTP_USER"): server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(msg)
    return True


def _issue_token(user_id, purpose, minutes=60):
    token = secrets.token_urlsafe(32)
    db = get_db()
    db.execute("INSERT INTO auth_tokens(user_id,token_hash,purpose,expires_at) VALUES(?,?,?,?)", (user_id, generate_password_hash(token), purpose, (datetime.now()+timedelta(minutes=minutes)).isoformat(timespec="seconds")))
    db.commit()
    return token


@app.before_request
def close_expired_auctions():
    if request.endpoint in ("static",): return
    db = get_db()
    rows = db.execute("SELECT id,title,created_by FROM auctions WHERE status='open'").fetchall()
    for a in rows:
        if _is_expired(db.execute("SELECT ends_at FROM auctions WHERE id=?", (a["id"],)).fetchone()[0]):
            db.execute("UPDATE auctions SET status='closed' WHERE id=?", (a["id"],))
            winner = db.execute("SELECT user_id,amount FROM bids WHERE auction_id=? ORDER BY amount DESC,id ASC LIMIT 1", (a["id"],)).fetchone()
            if winner: _notify(winner["user_id"], f"You won the auction: {a['title']}!", url_for("auction_detail", auction_id=a["id"]))
            if a["created_by"]: _notify(a["created_by"], f"Your auction ended: {a['title']}.", url_for("auction_detail", auction_id=a["id"]))
    db.commit()


@app.route("/verify-email/<token>")
def verify_email(token):
    db=get_db(); tokens=db.execute("SELECT * FROM auth_tokens WHERE purpose='verify' AND used=0").fetchall()
    match=next((t for t in tokens if check_password_hash(t["token_hash"], token)), None)
    if not match or _is_expired(match["expires_at"]): flash("That verification link is invalid or expired. Request a new one by registering again or contacting support.", "danger")
    else:
        db.execute("UPDATE users SET email_verified=1 WHERE id=?", (match["user_id"],)); db.execute("UPDATE auth_tokens SET used=1 WHERE id=?", (match["id"],)); db.commit(); flash("Email verified successfully.", "success")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET","POST"])
def forgot_password():
    if request.method=="POST":
        email=request.form.get("email","").strip().lower(); user=get_db().execute("SELECT id,email FROM users WHERE email=?",(email,)).fetchone()
        # Generic response avoids disclosing whether an account exists.
        if user:
            token=_issue_token(user["id"],"reset",30); link=url_for("reset_password",token=token,_external=True)
            try: _send_email(email,"GoldenHammer password reset",f"Reset your password within 30 minutes: {link}")
            except Exception: app.logger.exception("Password reset email failed")
            if not os.environ.get("SMTP_HOST"): flash("Mail is not configured. For local development only, reset link: "+link,"warning")
        flash("If that email is registered, password-reset instructions have been sent.","info")
    return render_template("simple_form.html", page_title="Reset password", form_kind="forgot")


@app.route("/reset-password/<token>", methods=["GET","POST"])
def reset_password(token):
    db=get_db(); rows=db.execute("SELECT * FROM auth_tokens WHERE purpose='reset' AND used=0").fetchall(); match=next((r for r in rows if check_password_hash(r["token_hash"],token)),None)
    if not match or _is_expired(match["expires_at"]): flash("This reset link is invalid or expired.","danger"); return redirect(url_for("forgot_password"))
    if request.method=="POST":
        password=request.form.get("password",""); confirm=request.form.get("confirm_password","")
        if len(password)<8 or password!=confirm: flash("Use at least 8 characters and make sure both passwords match.","danger")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",(generate_password_hash(password),match["user_id"])); db.execute("UPDATE auth_tokens SET used=1 WHERE id=?",(match["id"],)); db.commit(); flash("Password updated. Please sign in.","success"); return redirect(url_for("login"))
    return render_template("simple_form.html",page_title="Choose a new password",form_kind="reset")


@app.route("/notifications")
@login_required
def notifications():
    rows=get_db().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",(current_user()["id"],)).fetchall()
    return render_template("simple_form.html",page_title="Notifications",form_kind="notifications",items=rows)


@app.post("/notifications/read-all")
@login_required
def notifications_read_all():
    db=get_db(); db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?",(current_user()["id"],)); db.commit(); return redirect(url_for("notifications"))


@app.route("/favorites")
@login_required
def favorites():
    items=get_db().execute("SELECT a.* FROM favorites f JOIN auctions a ON a.id=f.auction_id WHERE f.user_id=? ORDER BY f.created_at DESC",(current_user()["id"],)).fetchall()
    return render_template("index.html",auctions=items,selected_category="",search="",favorites_page=True)


@app.post("/auction/<int:auction_id>/favorite")
@login_required
def toggle_favorite(auction_id):
    db=get_db(); user=current_user()
    if not db.execute("SELECT id FROM auctions WHERE id=?",(auction_id,)).fetchone(): abort(404)
    found=db.execute("SELECT 1 FROM favorites WHERE user_id=? AND auction_id=?",(user["id"],auction_id)).fetchone()
    if found: db.execute("DELETE FROM favorites WHERE user_id=? AND auction_id=?",(user["id"],auction_id)); flash("Removed from watchlist.","success")
    else: db.execute("INSERT OR IGNORE INTO favorites(user_id,auction_id) VALUES(?,?)",(user["id"],auction_id)); flash("Added to your watchlist.","success")
    db.commit(); return redirect(request.referrer or url_for("auction_detail",auction_id=auction_id))


@app.post("/auction/<int:auction_id>/report")
@login_required
def report_auction(auction_id):
    reason=request.form.get("reason","Other")[:80]; details=request.form.get("details","").strip()[:1000]
    db=get_db()
    if not db.execute("SELECT id FROM auctions WHERE id=?",(auction_id,)).fetchone(): abort(404)
    db.execute("INSERT INTO reports(reporter_id,auction_id,reason,details) VALUES(?,?,?,?)",(current_user()["id"],auction_id,reason,details)); db.commit(); flash("Report sent to moderation for review.","success"); return redirect(url_for("auction_detail",auction_id=auction_id))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    reports=get_db().execute("SELECT r.*,u.name AS reporter,a.title AS product FROM reports r JOIN users u ON u.id=r.reporter_id JOIN auctions a ON a.id=r.auction_id ORDER BY CASE r.status WHEN 'open' THEN 0 ELSE 1 END,r.id DESC").fetchall()
    return render_template("simple_form.html",page_title="Marketplace reports",form_kind="reports",items=reports)


@app.post("/admin/reports/<int:report_id>/resolve")
@admin_required
def resolve_report(report_id):
    db=get_db(); db.execute("UPDATE reports SET status='reviewed' WHERE id=?",(report_id,)); db.commit(); flash("Report marked reviewed.","success"); return redirect(url_for("admin_reports"))


@app.route("/admin/user-reports")
@admin_required
def admin_user_reports():
    db=get_db()
    db.execute("CREATE TABLE IF NOT EXISTS user_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL REFERENCES users(id), reported_user_id INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    rows=db.execute("SELECT r.*, reporter.name AS reporter_name, target.name AS reported_name FROM user_reports r JOIN users reporter ON reporter.id=r.reporter_id JOIN users target ON target.id=r.reported_user_id ORDER BY CASE r.status WHEN 'open' THEN 0 ELSE 1 END,r.id DESC").fetchall()
    return render_template("admin_user_reports.html", reports=rows)


@app.post("/admin/user-reports/<int:report_id>/resolve")
@admin_required
def resolve_user_report(report_id):
    db=get_db(); db.execute("UPDATE user_reports SET status='reviewed' WHERE id=?", (report_id,)); db.commit(); flash("User report marked reviewed.", "success"); return redirect(url_for("admin_user_reports"))


@app.route("/seller/<int:user_id>")
def seller_profile(user_id):
    db=get_db(); seller=db.execute("SELECT id,name,created_at FROM users WHERE id=? AND role='user'",(user_id,)).fetchone()
    if not seller: abort(404)
    profile=db.execute("SELECT * FROM seller_profiles WHERE user_id=?",(user_id,)).fetchone()
    listings=db.execute("SELECT * FROM auctions WHERE created_by=? AND status='open' ORDER BY id DESC",(user_id,)).fetchall()
    return render_template("simple_form.html",page_title=f"Seller: {seller['name']}",form_kind="seller",seller=seller,profile=profile,items=listings)


@app.route("/seller-profile",methods=["GET","POST"])
@login_required
def edit_seller_profile():
    user=current_user(); db=get_db()
    if request.method=="POST":
        bio=request.form.get("bio","").strip()[:1000]; location=request.form.get("location","").strip()[:120]
        db.execute("INSERT INTO seller_profiles(user_id,bio,location) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET bio=excluded.bio,location=excluded.location,updated_at=CURRENT_TIMESTAMP",(user["id"],bio,location)); db.commit(); flash("Seller profile saved.","success"); return redirect(url_for("edit_seller_profile"))
    profile=db.execute("SELECT * FROM seller_profiles WHERE user_id=?",(user["id"],)).fetchone()
    return render_template("simple_form.html",page_title="Seller profile",form_kind="edit_seller",profile=profile)


@app.post("/products/<int:auction_id>/image")
@login_required
def upload_product_image(auction_id):
    user=current_user(); db=get_db(); item=db.execute("SELECT * FROM auctions WHERE id=? AND created_by=?",(auction_id,user["id"])).fetchone()
    if not item: abort(404)
    f=request.files.get("image")
    if not f or "." not in f.filename or f.filename.rsplit(".",1)[1].lower() not in ALLOWED_IMAGE_EXTENSIONS: flash("Upload a PNG, JPG, WEBP, or GIF image.","danger")
    else:
        name=secure_filename(f"item_{auction_id}_{secrets.token_hex(8)}."+f.filename.rsplit(".",1)[1].lower()); f.save(UPLOAD_DIR/name)
        db.execute("UPDATE auctions SET image_url=?,image_filename=? WHERE id=?",(url_for("static",filename="uploads/"+name),name,auction_id)); db.commit(); flash("Product image uploaded.","success")
    return redirect(url_for("user_edit_product",auction_id=auction_id))


@app.route("/checkout/<int:auction_id>",methods=["GET","POST"])
@login_required
def checkout(auction_id):
    db=get_db(); item=db.execute("SELECT * FROM auctions WHERE id=?",(auction_id,)).fetchone()
    if not item or item["status"]!="closed": flash("Checkout is available only for closed auctions.","warning"); return redirect(url_for("auction_detail",auction_id=auction_id))
    winner=db.execute("SELECT user_id,amount FROM bids WHERE auction_id=? ORDER BY amount DESC,id ASC LIMIT 1",(auction_id,)).fetchone()
    if not winner or winner["user_id"]!=current_user()["id"]: abort(403)
    if request.method=="POST":
        # Safe order record only; real card/payment processing requires a configured payment-provider account.
        cur=db.execute("INSERT INTO orders(auction_id,buyer_id,seller_id,amount,status) VALUES(?,?,?,?, 'pending')",(auction_id,current_user()["id"],item["created_by"] or current_user()["id"],winner["amount"])); db.commit(); flash("Order created as pending. No payment was charged; configure a payment provider before accepting real payments.","warning"); return redirect(url_for("my_orders"))
    return render_template("simple_form.html",page_title="Checkout",form_kind="checkout",item=item,winner=winner)


@app.route("/my-orders")
@login_required
def my_orders():
    user=current_user(); rows=get_db().execute("SELECT o.*,a.title FROM orders o JOIN auctions a ON a.id=o.auction_id WHERE o.buyer_id=? OR o.seller_id=? ORDER BY o.id DESC",(user["id"],user["id"])).fetchall()
    return render_template("simple_form.html",page_title="Orders",form_kind="orders",items=rows)


@app.post("/orders/<int:order_id>/dispute")
@login_required
def create_dispute(order_id):
    details=request.form.get("details","").strip()[:2000]
    if len(details)<10: flash("Please describe the issue in at least 10 characters.","danger"); return redirect(url_for("my_orders"))
    db=get_db(); order=db.execute("SELECT * FROM orders WHERE id=? AND (buyer_id=? OR seller_id=?)",(order_id,current_user()["id"],current_user()["id"])).fetchone()
    if not order: abort(404)
    db.execute("INSERT INTO disputes(order_id,user_id,details) VALUES(?,?,?)",(order_id,current_user()["id"],details)); db.commit(); flash("Dispute submitted for administrator review.","success"); return redirect(url_for("my_orders"))


@app.route("/admin/backup")
@admin_required
def admin_backup():
    # Backups are for local/single-instance SQLite only. Production PostgreSQL backups should be handled by the provider.
    if not DATABASE.exists(): abort(404)
    from flask import send_file
    backup=BASE_DIR / f"goldenhammer-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    src=sqlite3.connect(DATABASE); dst=sqlite3.connect(backup)
    with dst: src.backup(dst)
    dst.close(); src.close()
    return send_file(backup,as_attachment=True,download_name=backup.name)



@app.route("/people")
@login_required
def people_search():
    user = current_user()
    if user["role"] != "user":
        abort(403)
    query = request.args.get("q", "").strip()[:80]
    db = get_db()
    if query:
        people = db.execute("SELECT id,name,created_at FROM users WHERE role='user' AND id<>? AND name LIKE ? ORDER BY name LIMIT 50", (user["id"], f"%{query}%")).fetchall()
    else:
        people = db.execute("SELECT id,name,created_at FROM users WHERE role='user' AND id<>? ORDER BY name LIMIT 50", (user["id"],)).fetchall()
    followed = {r[0] for r in db.execute("SELECT followed_id FROM user_follows WHERE follower_id=?", (user["id"],)).fetchall()}
    blocked = {r[0] for r in db.execute("SELECT blocked_id FROM user_blocks WHERE blocker_id=?", (user["id"],)).fetchall()}
    return render_template("people.html", people=people, query=query, followed=followed, blocked=blocked)


@app.route("/messages")
@login_required
def messages_home():
    user = current_user()
    if user["role"] != "user": abort(403)
    db = get_db()
    rows = db.execute("""SELECT u.id,u.name, MAX(m.id) AS last_id,
        (SELECT body FROM direct_messages x WHERE (x.sender_id=u.id AND x.recipient_id=?) OR (x.sender_id=? AND x.recipient_id=u.id) ORDER BY x.id DESC LIMIT 1) AS last_message,
        (SELECT COUNT(*) FROM direct_messages x WHERE x.sender_id=u.id AND x.recipient_id=? AND x.is_read=0) AS unread
        FROM users u JOIN direct_messages m ON (m.sender_id=u.id AND m.recipient_id=?) OR (m.sender_id=? AND m.recipient_id=u.id)
        WHERE u.role='user' AND u.id<>? GROUP BY u.id ORDER BY last_id DESC""", (user["id"], user["id"], user["id"], user["id"], user["id"], user["id"])).fetchall()
    return render_template("messages.html", conversations=rows)


@app.route("/messages/<int:other_id>")
@login_required
def direct_chat(other_id):
    user = current_user()
    if user["role"] != "user" or other_id == user["id"]: abort(403)
    other = get_db().execute("SELECT id,name FROM users WHERE id=? AND role='user'", (other_id,)).fetchone()
    if not other: abort(404)
    db = get_db()
    if db.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)", (user["id"], other_id, other_id, user["id"])).fetchone():
        flash("Messaging is unavailable because one account has blocked the other.", "warning")
        return redirect(url_for("people_search"))
    db.execute("UPDATE direct_messages SET is_read=1 WHERE sender_id=? AND recipient_id=?", (other_id, user["id"])); db.commit()
    return render_template("chat.html", other=other, csrf=csrf_token())


@app.route("/api/chat/<int:other_id>", methods=["GET", "POST"])
@login_required
def chat_api(other_id):
    user = current_user()
    if user["role"] != "user" or other_id == user["id"]: abort(403)
    db = get_db()
    other = db.execute("SELECT id FROM users WHERE id=? AND role='user'", (other_id,)).fetchone()
    if not other: abort(404)
    blocked = db.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)", (user["id"], other_id, other_id, user["id"])).fetchone()
    if blocked: return jsonify(error="Messaging unavailable due to a block."), 403
    if request.method == "POST":
        import hmac
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied): abort(400)
        data = request.get_json(silent=True) or {}
        body = str(data.get("body", "")).strip()[:2000]
        product_url = str(data.get("product_url", "")).strip()[:500]
        if not body and not product_url: return jsonify(error="Write a message or share a product link."), 400
        if product_url and not (product_url.startswith("/") and not product_url.startswith("//")):
            return jsonify(error="Product links must be internal GoldenHammer links."), 400
        cur = db.execute("INSERT INTO direct_messages(sender_id,recipient_id,body,product_url) VALUES(?,?,?,?)", (user["id"], other_id, body, product_url))
        db.commit()
        payload = dict(id=cur.lastrowid, sender_id=user["id"], recipient_id=other_id, body=body, product_url=product_url, created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        socketio.emit("direct_message", payload, to=f"user_{other_id}")
        socketio.emit("direct_message", payload, to=f"user_{user['id']}")
        return jsonify(payload), 201
    after = request.args.get("after", "0")
    try: after_id = max(0, int(after))
    except ValueError: after_id = 0
    db.execute("UPDATE direct_messages SET is_read=1 WHERE sender_id=? AND recipient_id=? AND is_read=0", (other_id, user["id"]))
    rows = db.execute("SELECT id,sender_id,recipient_id,body,product_url,is_read,created_at FROM direct_messages WHERE ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?)) AND id>? ORDER BY id LIMIT 100", (user["id"], other_id, other_id, user["id"], after_id)).fetchall()
    db.commit()
    return jsonify(messages=[dict(r) for r in rows], current_user_id=user["id"])


@app.post("/users/<int:other_id>/follow")
@login_required
def toggle_follow(other_id):
    user=current_user(); db=get_db()
    if user["role"] != "user" or other_id == user["id"]: abort(403)
    if not db.execute("SELECT id FROM users WHERE id=? AND role='user'", (other_id,)).fetchone(): abort(404)
    existing=db.execute("SELECT 1 FROM user_follows WHERE follower_id=? AND followed_id=?", (user["id"], other_id)).fetchone()
    if existing: db.execute("DELETE FROM user_follows WHERE follower_id=? AND followed_id=?", (user["id"], other_id)); flash("Seller unfollowed.", "success")
    else: db.execute("INSERT OR IGNORE INTO user_follows(follower_id,followed_id) VALUES(?,?)", (user["id"], other_id)); flash("Seller followed.", "success")
    db.commit(); return redirect(request.referrer if request.referrer and request.referrer.startswith("/") and not request.referrer.startswith("//") else url_for("people_search"))


@app.post("/users/<int:other_id>/block")
@login_required
def toggle_block(other_id):
    user=current_user(); db=get_db()
    if user["role"] != "user" or other_id == user["id"]: abort(403)
    if not db.execute("SELECT id FROM users WHERE id=? AND role='user'", (other_id,)).fetchone(): abort(404)
    existing=db.execute("SELECT 1 FROM user_blocks WHERE blocker_id=? AND blocked_id=?", (user["id"], other_id)).fetchone()
    if existing: db.execute("DELETE FROM user_blocks WHERE blocker_id=? AND blocked_id=?", (user["id"], other_id)); flash("User unblocked.", "success")
    else: db.execute("INSERT OR IGNORE INTO user_blocks(blocker_id,blocked_id) VALUES(?,?)", (user["id"], other_id)); flash("User blocked. Messaging between these accounts is disabled.", "success")
    db.commit(); return redirect(request.referrer if request.referrer and request.referrer.startswith("/") and not request.referrer.startswith("//") else url_for("people_search"))


@app.post("/users/<int:other_id>/report")
@login_required
def report_user(other_id):
    user=current_user(); db=get_db()
    if user["role"] != "user" or other_id == user["id"]: abort(403)
    if not db.execute("SELECT id FROM users WHERE id=? AND role='user'", (other_id,)).fetchone(): abort(404)
    reason=request.form.get("reason", "Harassment").strip()[:100]
    details=request.form.get("details", "").strip()[:1000]
    # Reuse moderation reports with nullable auction support via a dedicated table.
    db.execute("CREATE TABLE IF NOT EXISTS user_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL REFERENCES users(id), reported_user_id INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    db.execute("INSERT INTO user_reports(reporter_id,reported_user_id,reason,details) VALUES(?,?,?,?)", (user["id"], other_id, reason, details)); db.commit()
    flash("User report sent to the moderation queue.", "success")
    return redirect(url_for("people_search"))


@app.get("/api/auction/<int:auction_id>/live")
def live_auction_api(auction_id):
    db=get_db()
    auction=db.execute("SELECT id,title,current_bid,starting_price,ends_at,status,min_increment FROM auctions WHERE id=?", (auction_id,)).fetchone()
    if not auction: abort(404)
    bids=db.execute("SELECT b.id,b.amount,b.created_at,u.name,b.user_id FROM bids b JOIN users u ON u.id=b.user_id WHERE b.auction_id=? ORDER BY b.id DESC LIMIT 30", (auction_id,)).fetchall()
    count=db.execute("SELECT COUNT(DISTINCT user_id) FROM bids WHERE auction_id=?", (auction_id,)).fetchone()[0]
    leader=db.execute("SELECT u.name,b.amount,b.user_id FROM bids b JOIN users u ON u.id=b.user_id WHERE b.auction_id=? ORDER BY b.amount DESC,b.id ASC LIMIT 1", (auction_id,)).fetchone()
    return jsonify(auction=dict(auction), bidder_count=count, leader=dict(leader) if leader else None, bids=[dict(b) for b in bids])


@app.get("/api/auction/<int:auction_id>/chat")
@login_required
def auction_chat_history(auction_id):
    db = get_db()
    if not db.execute("SELECT id FROM auctions WHERE id=?", (auction_id,)).fetchone(): abort(404)
    rows = db.execute("SELECT m.id,m.user_id,u.name,m.body,m.created_at FROM auction_chat_messages m JOIN users u ON u.id=m.user_id WHERE m.auction_id=? ORDER BY m.id DESC LIMIT 60", (auction_id,)).fetchall()
    return jsonify(messages=[dict(r) for r in reversed(rows)])

@app.post("/api/auction/<int:auction_id>/chat")
@login_required
def auction_chat_send(auction_id):
    user = current_user()
    if user["role"] != "user": return jsonify(error="Only signed-in marketplace users can chat."), 403
    db = get_db()
    auction = db.execute("SELECT id,status,auction_mode,host_live FROM auctions WHERE id=?", (auction_id,)).fetchone()
    if not auction: abort(404)
    if auction["status"] != "open": return jsonify(error="This auction chat is closed."), 400
    body = str(request.get_json(silent=True).get("body", "") if request.is_json else request.form.get("body", "")).strip()[:1000]
    if not body: return jsonify(error="Write a message first."), 400
    blocked = db.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?) LIMIT 1", (user["id"], auction_id, auction_id, user["id"])).fetchone()
    # Auction room messages are visible to participants; standard user block rules are enforced for direct messages.
    cur = db.execute("INSERT INTO auction_chat_messages(auction_id,user_id,body) VALUES(?,?,?)", (auction_id,user["id"],body)); db.commit()
    row = db.execute("SELECT m.id,m.user_id,u.name,m.body,m.created_at FROM auction_chat_messages m JOIN users u ON u.id=m.user_id WHERE m.id=?", (cur.lastrowid,)).fetchone()
    payload = dict(row)
    socketio.emit("auction_chat_message", payload, to=f"auction_{auction_id}")
    return jsonify(payload), 201

@app.post("/auction/<int:auction_id>/host-control")
@login_required
def auction_host_control(auction_id):
    user = current_user(); db = get_db()
    auction = db.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
    if not auction: abort(404)
    if user["role"] != "admin" and auction["created_by"] != user["id"]: abort(403)
    if auction["auction_mode"] != "hosted":
        flash("Only host-led auctions have host controls.", "warning")
        return redirect(url_for("auction_detail", auction_id=auction_id))
    action = request.form.get("action")
    if action == "start" and auction["status"] == "open":
        db.execute("UPDATE auctions SET host_live=1,host_started_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), auction_id))
        flash("Your live auction room is now marked live.", "success")
    elif action == "pause":
        db.execute("UPDATE auctions SET host_live=0 WHERE id=?", (auction_id,))
        flash("Host-led auction paused. Bidding remains available until the scheduled end.", "warning")
    elif action == "close":
        db.execute("UPDATE auctions SET status='closed',host_live=0 WHERE id=?", (auction_id,))
        winner = db.execute("SELECT user_id FROM bids WHERE auction_id=? ORDER BY amount DESC,id ASC LIMIT 1", (auction_id,)).fetchone()
        if winner: _notify(winner["user_id"], f"You won the hosted auction: {auction['title']}!", url_for("auction_detail", auction_id=auction_id))
        flash("Host-led auction closed.", "success")
    else:
        flash("That host action is not available.", "danger")
    db.commit()
    socketio.emit("auction_state", {"auction_id": auction_id, "action": action}, to=f"auction_{auction_id}")
    return redirect(url_for("auction_detail", auction_id=auction_id))

@socketio.on("join_user")
def socket_join_user(data=None):
    user_id = session.get("user_id")
    if user_id:
        join_room(f"user_{user_id}")

@socketio.on("join_auction")
def socket_join_auction(data):
    user_id = session.get("user_id")
    auction_id = int((data or {}).get("auction_id", 0))
    if not user_id or not get_db().execute("SELECT id FROM auctions WHERE id=?", (auction_id,)).fetchone():
        return
    join_room(f"auction_{auction_id}")
    emit("auction_joined", {"auction_id": auction_id})

@app.route("/terms")
def terms(): return render_template("simple_form.html",page_title="Terms of service",form_kind="terms")
@app.route("/privacy")
def privacy(): return render_template("simple_form.html",page_title="Privacy policy",form_kind="privacy")
@app.route("/prohibited-items")
def prohibited_items(): return render_template("simple_form.html",page_title="Prohibited items",form_kind="prohibited")


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html",code=403,message="You do not have permission to view this page."),403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html",code=404,message="That page or auction could not be found."),404


# Initialize tables for both local execution and hosted WSGI servers (for example Gunicorn).
init_db()

if __name__ == "__main__":
    print("GoldenHammer is starting at http://127.0.0.1:5001")
    print("Demo admin: admin@goldenhammer.local / ChangeMe123!")
    socketio.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5001")),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
        allow_unsafe_werkzeug=True
    )