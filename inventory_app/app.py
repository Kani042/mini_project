import os, uuid, sys
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
from passlib.hash import pbkdf2_sha256
from dotenv import load_dotenv
from .db import get_db, init_db
import time

# Only load local .env when running in non-production and DATABASE_URL is not provided.
if os.environ.get("FLASK_ENV", "").lower() != "production" and not os.environ.get("DATABASE_URL"):
    load_dotenv()
    print("Loaded .env for local development")
else:
    print("Running in production or DATABASE_URL present — skipping .env load")

# Use module-relative absolute paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))

print("Template folder being used:", app.template_folder)
print("Static folder being used:", app.static_folder)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

# Initialize database on app startup
try:
  init_db()
  print("✓ Database initialized successfully!")
except Exception as e:
  print(f"✗ Database initialization error: {e}")
  # don't exit in production; keep app running so we can inspect logs
  # sys.exit(1)

# Session timeout (seconds) — default 3600 (1 hour).
# Configure via environment variable SESSION_TIMEOUT (seconds) if you want longer/shorter.
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "3600"))

# -------- Helpers --------
def require_admin():
  """Return True if admin session is active and not expired; otherwise clear session and require login."""
  admin_id = session.get("admin_id")
  if not admin_id:
    # debug print to see session contents on every protected page hit
    print("require_admin: no admin_id in session. session keys:", dict(session))
    flash("Please log in as admin.")
    return False

  # Check last-active timestamp for inactivity timeout
  try:
    last = session.get("_last_active")
    if last is None:
      # no timestamp -> consider session stale
      print("require_admin: no last_active timestamp, clearing session")
      session.clear()
      flash("Session expired. Please log in again.")
      return False
    now = int(time.time())
    if (now - int(last)) > SESSION_TIMEOUT:
      print(f"require_admin: session expired (now={now}, last={last}, timeout={SESSION_TIMEOUT})")
      session.clear()
      flash("Session expired. Please log in again.")
      return False
    # update last-active to extend session on activity (sliding window)
    session["_last_active"] = now
    session.modified = True
  except Exception as e:
    print("require_admin: timestamp check error:", e)
    # on error, clear session defensively
    session.clear()
    flash("Session error. Please log in again.")
    return False

  # debug print to confirm admin id present and session active
  print("require_admin: admin_id:", admin_id, "last_active:", session.get("_last_active"))
  return True

def current_stock(conn, item_id):
  cur = conn.execute("SELECT COALESCE(SUM(delta_quantity),0) AS qty FROM inventory_stock WHERE item_id=%s", (item_id,))
  return cur.fetchone()["qty"]

# -------- Auth --------
@app.route("/register", methods=["GET", "POST"])
def register():
    conn = get_db()
    if request.method == "POST":
        # ensure stored emails are normalized to lowercase
        email = request.form["email"].lower().strip()
        password = request.form["password"]
        if not email or not password:
          flash("Email and password required.")
          return redirect(url_for("register"))
        pw_hash = pbkdf2_sha256.hash(password)
        try:
            # insert normalized email
            conn.execute("INSERT INTO admins (email, password_hash) VALUES (%s, %s)", (email, pw_hash))
            conn.commit()
            flash("Admin registered. Please log in.")
            return redirect(url_for("login"))
        except Exception:
            flash("Registration failed. Email may already exist.")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        password = request.form["password"]
        print(f"Login attempt for email: {email}")
        try:
            admin = conn.execute("SELECT * FROM admins WHERE email=?", (email,)).fetchone()
        except Exception as e:
            print("login: DB select error:", e)
            admin = None

        print("login: fetched admin row:", admin)

        if not admin:
            print(f"No admin found with email: {email}")
            flash("Invalid credentials.")
        else:
            # ...existing password verification code...
            stored_hash = admin.get("password_hash") if isinstance(admin, dict) else (admin["password_hash"] if admin else None)
            try:
                verified = pbkdf2_sha256.verify(password, stored_hash)
            except Exception as e:
                print("login: verify error:", e)
                verified = False

            if verified:
                print(f"Login successful for: {email}")
                # ensure admin_id stored in session
                admin_id_resolved = admin.get("id") if isinstance(admin, dict) else admin["id"]
                session["admin_id"] = admin_id_resolved
                session["admin_email"] = admin.get("email") if isinstance(admin, dict) else admin["email"]
                # set last-active timestamp for session timeout sliding window
                session["_last_active"] = int(time.time())
                session.permanent = True
                session.modified = True

                # ensure Flask writes the session cookie into the redirect response immediately
                from flask import make_response
                resp = make_response(redirect(url_for("dashboard")))
                try:
                    app.session_interface.save_session(app, session, resp)
                except Exception as e:
                    print("login: save_session failed:", e)
                print("login: session after set:", dict(session))
                return resp
            else:
                print(f"Password verification failed for: {email}")
                flash("Invalid credentials.")
    return render_template("login.html")

@app.route("/logout")
def logout():
  session.clear()
  flash("Logged out.")
  return redirect(url_for("login"))

# -------- Dashboard --------
@app.route("/")
def dashboard():
  if not require_admin():
    return redirect(url_for("login"))
  conn = get_db()
  admin_id = session.get("admin_id")
  items = conn.execute("SELECT * FROM inventory_items WHERE admin_id=%s ORDER BY created_at DESC", (admin_id,)).fetchall()
  stocks = {item["id"]: current_stock(conn, item["id"]) for item in items}
  return render_template("dashboard.html", items=items, stocks=stocks)

# -------- Inventory management --------
@app.route("/inventory/new", methods=["GET", "POST"])
def inventory_new():
  if not require_admin():
    return redirect(url_for("login"))
  conn = get_db()
  if request.method == "POST":
    sku = request.form["sku"].strip()
    name = request.form["name"].strip()
    description = request.form.get("description", "").strip()
    unit_price = float(request.form["unit_price"])
    admin_id = session.get("admin_id")
    
    if not sku or not name:
      flash("SKU and name are required.")
      return redirect(url_for("inventory_new"))

    # Normalize SKU for comparison (trim + lower) and check uniqueness for this admin
    sku_norm = sku.strip().lower()
    try:
      # case-insensitive, trimmed comparison
      exists = conn.execute(
        "SELECT id, sku FROM inventory_items WHERE admin_id=%s AND LOWER(TRIM(sku)) = %s LIMIT 1",
        (admin_id, sku_norm)
      ).fetchone()
    except Exception as e:
      # log the DB error but continue to attempt insert (will be caught below)
      print("inventory_new: uniqueness check failed:", e)
      exists = None

    if exists:
      # helpful debug output for why duplication flagged
      try:
        print("inventory_new: duplicate SKU detected for admin_id=%s -> db row: %s" % (admin_id, dict(exists)))
      except Exception:
        print("inventory_new: duplicate SKU detected (could not stringify row).")
      flash("ProductCode must be unique.")
      return redirect(url_for("inventory_new"))
    
    try:
      conn.execute(
        "INSERT INTO inventory_items (sku, name, description, unit_price, admin_id) VALUES (%s, %s, %s, %s, %s)",
        (sku, name, description, unit_price, admin_id)
      )
      conn.commit()
      flash("Item created.")
      return redirect(url_for("dashboard"))
    except Exception:
      import traceback, sys
      traceback.print_exc(file=sys.stderr)
      flash("ProductCode must be unique.")
  return render_template("inventory_new.html")

@app.route("/inventory/<int:item_id>/add_stock", methods=["POST"])
def add_stock(item_id):
  if not require_admin():
    return redirect(url_for("login"))
  conn = get_db()
  admin_id = session.get("admin_id")
  
  # Verify item belongs to this admin
  item = conn.execute("SELECT * FROM inventory_items WHERE id=%s AND admin_id=%s", (item_id, admin_id)).fetchone()
  if not item:
    flash("Item not found or access denied.")
    return redirect(url_for("dashboard"))
  
  qty = int(request.form["quantity"])
  reason = request.form.get("reason", "Add stock")
  conn.execute("INSERT INTO inventory_stock (item_id, delta_quantity, reason) VALUES (%s, %s, %s)",
               (item_id, qty, reason))
  conn.commit()
  flash("Stock updated.")
  return redirect(url_for("dashboard"))

# -------- Checkout - New Flow --------
@app.route("/checkout", methods=["GET", "POST"])
def checkout():
  if not require_admin():
    return redirect(url_for("login"))

  if request.method == "POST":
    conn = get_db()               # single DBConnection for this request
    try:
      # Customer details submission
      mobile = request.form["mobile"].strip()
      name = request.form.get("name", "").strip()
      tax_rate = float(request.form.get("tax_rate", "0"))
      payment_mode = request.form.get("payment_mode", "Cash")

      cart = session.get("checkout_cart", [])
      if not cart:
        flash("No items in cart.")
        return redirect(url_for("checkout"))

      admin_id = session.get("admin_id")

      # Validate stock (reads use same conn)
      for cart_item in cart:
        available_row = current_stock(conn, cart_item["item_id"])
        # current_stock returns a number (ensure consistent)
        if isinstance(available_row, dict) and "qty" in available_row:
          available = available_row["qty"]
        else:
          available = available_row
        if cart_item["qty"] > available:
          flash(f"Not enough stock for {cart_item['name']}. Available: {available}.")
          return redirect(url_for("checkout"))

      # Get or create user
      user = conn.execute("SELECT * FROM users WHERE mobile=%s", (mobile,)).fetchone()
      if not user:
        conn.execute("INSERT INTO users (mobile, name) VALUES (%s, %s)", (mobile, name))
        # do not commit yet — delay until entire transaction is successful
        user = conn.execute("SELECT * FROM users WHERE mobile=%s", (mobile,)).fetchone()

      # Calculate totals
      subtotal = sum(item["unit_price"] * item["qty"] for item in cart)
      tax = round(subtotal * tax_rate, 2)
      total = round(subtotal + tax, 2)
      invoice_number = f"INV-{uuid.uuid4().hex[:10].upper()}"

      # Ensure payment_mode column exists (safe, idempotent)
      try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()] if conn.driver == "sqlite" else \
               [r["column_name"] for r in conn.execute(
                 "SELECT column_name FROM information_schema.columns WHERE table_name='invoices'").fetchall()]
        if "payment_mode" not in cols:
          try:
            conn.execute("ALTER TABLE invoices ADD COLUMN payment_mode TEXT")
          except Exception:
            pass
      except Exception:
        pass

      # Create invoice and ensure invoice_number is set at INSERT time.
      if conn.driver == "pg":
        # Postgres: reserve a sequence value from the invoices id sequence, build invoice_number, then insert with that id.
        try:
          seq_row = conn.execute("SELECT pg_get_serial_sequence('invoices','id') AS seq").fetchone()
          seq_name = seq_row.get("seq") if seq_row else None
        except Exception:
          seq_name = None

        if seq_name:
          # get next sequence value
          nid_row = conn.execute("SELECT nextval(%s) AS nid", (seq_name,)).fetchone()
          invoice_id = nid_row.get("nid") if nid_row else None
        else:
          invoice_id = None

        if invoice_id:
          invoice_number = f"INV-{int(invoice_id):06d}"
          conn.execute(
            "INSERT INTO invoices (id, invoice_number, user_id, subtotal, tax, total, admin_id, payment_mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (invoice_id, invoice_number, user["id"], subtotal, tax, total, admin_id, payment_mode)
          )
          conn.commit()
          # conn.commit() closes the Postgres connection inside DBConnection,
          # so open a fresh connection to read the inserted invoice
          #          fresh = get_db()
          #          try:
          #            invoice = fresh.execute("SELECT * FROM invoices WHERE id=%s", (invoice_id,)).fetchone()
          #          finally:
          #            try:
          #              fresh.rollback()
          #            except Exception:
          #              pass
          # use a fresh connection for further work (select + remaining inserts)
          conn = get_db()
          invoice = conn.execute("SELECT * FROM invoices WHERE id=%s", (invoice_id,)).fetchone()
        else:
          # Fallback: insert with RETURNING id and compute invoice_number afterwards (rare)
          row = conn.execute(
            "INSERT INTO invoices (user_id, subtotal, tax, total, admin_id, payment_mode) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (user["id"], subtotal, tax, total, admin_id, payment_mode)
          ).fetchone()
          invoice_id = row["id"] if row else None
          invoice_number = f"INV-{int(invoice_id):06d}" if invoice_id else None
          if invoice_id and invoice_number:
            conn.execute("UPDATE invoices SET invoice_number=%s WHERE id=%s", (invoice_number, invoice_id))
            conn.commit()
          # commit above may have closed connection; use fresh connection for subsequent work
          conn = get_db()
          invoice = conn.execute("SELECT * FROM invoices WHERE id=%s", (invoice_id,)).fetchone()
      else:
        # SQLite: insert then read last_insert_rowid(), then update invoice_number
        conn.execute(
          "INSERT INTO invoices (user_id, subtotal, tax, total, admin_id, payment_mode) VALUES (?, ?, ?, ?, ?, ?)",
          (user["id"], subtotal, tax, total, admin_id, payment_mode)
        )
        conn.commit()
        invoice_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone().get("id")
        invoice_number = f"INV-{int(invoice_id):06d}" if invoice_id else None
        if invoice_id and invoice_number:
          conn.execute("UPDATE invoices SET invoice_number=? WHERE id=?", (invoice_number, invoice_id))
          conn.commit()
        # For SQLite the same connection is still valid; read directly
        invoice_row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        invoice = invoice_row.fetchone() if hasattr(invoice_row, "fetchone") else invoice_row

      # Add invoice items and update stock (all on same conn)
      for cart_item in cart:
        item_id = cart_item["item_id"]
        qty = cart_item["qty"]
        unit_price = cart_item["unit_price"]
        line_total = round(unit_price * qty, 2)

        # Get current stock before reduction
        stock_before_row = conn.execute("SELECT COALESCE(SUM(delta_quantity),0) AS qty FROM inventory_stock WHERE item_id=%s", (item_id,)).fetchone()
        stock_before = stock_before_row.get("qty") if isinstance(stock_before_row, dict) else (stock_before_row["qty"] if stock_before_row else 0)

        # Add invoice item
        conn.execute(
          "INSERT INTO invoice_items (invoice_id, item_id, quantity, unit_price, line_total) VALUES (%s, %s, %s, %s, %s)",
          (invoice["id"], item_id, qty, unit_price, line_total)
        )

        # Reduce stock by adding negative delta
        conn.execute(
          "INSERT INTO inventory_stock (item_id, delta_quantity, reason) VALUES (%s, %s, %s)",
          (item_id, -qty, f"Sold - {invoice_number}")
        )

      # All operations succeeded — commit once
      conn.commit()

      # Clear cart from session
      session.pop("checkout_cart", None)
      return redirect(url_for("invoice_view", invoice_id=invoice["id"]))
    except Exception as e:
      # rollback and log original exception for debugging
      try:
        conn.rollback()
      except Exception:
        pass
      import traceback, sys
      traceback.print_exc(file=sys.stderr)
      flash("An error occurred while generating the invoice. Check server logs.")
      return redirect(url_for("checkout"))
  	
  # GET request - show cart
  cart = session.get("checkout_cart", [])
  subtotal = sum(item["unit_price"] * item["qty"] for item in cart)
  return render_template("checkout.html", cart=cart, subtotal=subtotal)

# -------- Checkout search (case-insensitive) --------
@app.route("/checkout/search", methods=["GET"])
def checkout_search():
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401

    query = request.args.get("q", "").strip()
    admin_id = session.get("admin_id")
    if not query or len(query) < 3:
        return jsonify([])

    conn = get_db()
    try:
        if conn.driver == "pg":
            # Postgres: use ILIKE for case-insensitive pattern match
            items = conn.execute("""
                SELECT id, sku, name, unit_price
                FROM inventory_items
                WHERE admin_id=%s AND (sku ILIKE %s OR name ILIKE %s)
                ORDER BY name
            """, (admin_id, f"%{query}%", f"%{query}%")).fetchall()
        else:
            # SQLite: use LOWER(...) comparison
            items = conn.execute("""
                SELECT id, sku, name, unit_price
                FROM inventory_items
                WHERE admin_id=? AND (LOWER(sku) LIKE LOWER(?) OR LOWER(name) LIKE LOWER(?))
                ORDER BY name
            """, (admin_id, f"%{query}%", f"%{query}%")).fetchall()

        result = []
        for item in items:
            stock = current_stock(conn, item["id"])
            result.append({
                "id": item["id"],
                "sku": item["sku"],
                "name": item["name"],
                "unit_price": float(item["unit_price"]),
                "stock": stock
            })
        return jsonify(result)
    except Exception as e:
        print("checkout_search error:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/checkout/add-to-cart", methods=["POST"])
def add_to_cart():
  if not require_admin():
    return jsonify({"error": "Unauthorized"}), 401
  
  data = request.json
  item_id = data.get("item_id")
  qty = int(data.get("qty", 1))
  admin_id = session.get("admin_id")
  
  if qty <= 0:
    return jsonify({"error": "Invalid quantity"}), 400
  
  conn = get_db()
  # Verify item belongs to this admin
  item = conn.execute("SELECT * FROM inventory_items WHERE id=%s AND admin_id=%s", (item_id, admin_id)).fetchone()
  
  if not item:
    return jsonify({"error": "Item not found"}), 404
  
  stock = current_stock(conn, item_id)
  if qty > stock:
    return jsonify({"error": f"Only {stock} in stock"}), 400
  
  cart = session.get("checkout_cart", [])
  existing = next((c for c in cart if c["item_id"] == item_id), None)
  if existing:
    existing["qty"] += qty
  else:
    cart.append({
      "item_id": item_id,
      "sku": item["sku"],
      "name": item["name"],
      "unit_price": float(item["unit_price"]),
      "qty": qty
    })
  
  session["checkout_cart"] = cart
  return jsonify({"success": True, "cart_count": len(cart)})

@app.route("/checkout/remove-from-cart", methods=["POST"])
def remove_from_cart():
  if not require_admin():
    return jsonify({"error": "Unauthorized"}), 401
  
  data = request.json
  item_id = data.get("item_id")
  
  cart = session.get("checkout_cart", [])
  cart = [c for c in cart if c["item_id"] != item_id]
  session["checkout_cart"] = cart
  
  return jsonify({"success": True})

# -------- Invoice view --------
@app.route("/invoices/<int:invoice_id>")
def invoice_view(invoice_id):
  if not require_admin():
    return redirect(url_for("login"))
  conn = get_db()
  admin_id = session.get("admin_id")
  
  # Verify invoice belongs to this admin
  inv = conn.execute("SELECT * FROM invoices WHERE id=%s AND admin_id=%s", (invoice_id, admin_id)).fetchone()
  if not inv:
    flash("Invoice not found or access denied.")
    return redirect(url_for("dashboard"))
  
  user = conn.execute("SELECT * FROM users WHERE id=%s", (inv["user_id"],)).fetchone()
  lines = conn.execute("""
    SELECT ii.*, it.name, it.sku
    FROM invoice_items ii
    JOIN inventory_items it ON it.id = ii.item_id
    WHERE ii.invoice_id=%s
  """, (invoice_id,)).fetchall()
  return render_template("invoice.html", invoice=inv, user=user, lines=lines)

# -------- Orders List --------
@app.route("/orders")
def orders_list():
  if not require_admin():
    return redirect(url_for("login"))
  conn = get_db()
  admin_id = session.get("admin_id")
  
  # Get all invoices for this admin with customer details
  invoices = conn.execute("""
    SELECT i.*, u.mobile, u.name as customer_name
    FROM invoices i
    JOIN users u ON u.id = i.user_id
    WHERE i.admin_id = %s
    ORDER BY i.created_at DESC
  """, (admin_id,)).fetchall()
  
  return render_template("orders.html", invoices=invoices)

@app.route("/orders/<int:invoice_id>/items")
def order_items(invoice_id):
    if not require_admin():
        return redirect(url_for("login"))
    conn = get_db()
    admin_id = session.get("admin_id")

    # Verify invoice belongs to this admin
    inv = conn.execute("SELECT * FROM invoices WHERE id=%s AND admin_id=%s", (invoice_id, admin_id)).fetchone()
    if not inv:
        flash("Invoice not found or access denied.")
        return redirect(url_for("orders_list"))

    # Fetch invoice items with product details
    lines = conn.execute("""
      SELECT ii.*, it.name, it.sku
      FROM invoice_items ii
      JOIN inventory_items it ON it.id = ii.item_id
      WHERE ii.invoice_id=%s
      ORDER BY ii.id
    """, (invoice_id,)).fetchall()

    # Compute totals (defensive)
    total_qty = sum(int(l.get("quantity", 0)) for l in lines)
    total_amount = sum(float(l.get("line_total", 0) or 0) for l in lines)

    return render_template("order_items.html", invoice=inv, lines=lines, total_qty=total_qty, total_amount=total_amount)

@app.route("/analytics", methods=["GET"])
def analytics():
    if not require_admin():
        return redirect(url_for("login"))
    conn = get_db()
    admin_id = session.get("admin_id")

    # available payment modes for dropdown
    modes_rows = conn.execute(
        "SELECT DISTINCT COALESCE(NULLIF(payment_mode, ''), 'Unknown') AS payment_mode FROM invoices WHERE admin_id=%s ORDER BY payment_mode",
        (admin_id,)
    ).fetchall()
    payment_modes = [r["payment_mode"] for r in modes_rows] if modes_rows else []

    selected = request.args.get("payment_mode", "All")
    selected_date = request.args.get("date", "").strip()  # expected YYYY-MM-DD or empty

    # helper to build date filter and params depending on driver
    def _append_date_filter(params):
        if not selected_date:
            return "", params
        if conn.driver == "pg":
            return " AND DATE(i.created_at) = %s", params + [selected_date]
        else:
            return " AND DATE(created_at) = ?", params + [selected_date]

    # If "All" selected, produce a dict of payment_mode -> rows + totals
    if selected == "All":
        groups = {}
        grand = {"orders": 0, "total_qty": 0, "total_amount": 0.0}
        for pm in payment_modes:
            params = [admin_id]
            # mode filter param placeholder selection
            if conn.driver == "pg":
                mode_filter = " AND COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') = %s"
                params.append(pm)
                date_clause, params = _append_date_filter(params)
            else:
                mode_filter = " AND COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') = ?"
                params.append(pm)
                date_clause, params = _append_date_filter(params)

            rows = conn.execute(f"""
                SELECT
                    i.id,
                    i.invoice_number,
                    i.created_at,
                    COALESCE(u.name, '') AS customer_name,
                    COALESCE(SUM(ii.quantity),0) AS total_qty,
                    COALESCE(SUM(ii.line_total),0) AS total_amount,
                    COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') AS payment_mode
                FROM invoices i
                LEFT JOIN invoice_items ii ON ii.invoice_id = i.id
                LEFT JOIN users u ON u.id = i.user_id
                WHERE i.admin_id=%s {mode_filter} {date_clause}
                GROUP BY i.id, i.invoice_number, i.created_at, u.name, payment_mode
                ORDER BY i.created_at DESC
            """, tuple(params)).fetchall()

            totals = {"orders": 0, "total_qty": 0, "total_amount": 0.0}
            for r in rows:
                totals["orders"] += 1
                totals["total_qty"] += int(r["total_qty"] or 0)
                totals["total_amount"] += float(r["total_amount"] or 0.0)
            groups[pm] = {"rows": rows, "totals": totals}
            grand["orders"] += totals["orders"]
            grand["total_qty"] += totals["total_qty"]
            grand["total_amount"] += totals["total_amount"]

        return render_template("analytics.html", payment_modes=payment_modes, selected=selected, groups=groups, grand=grand, selected_date=selected_date)

    # else: single table behavior (filter by selected mode if not "All")
    params = [admin_id]
    mode_filter = ""
    if selected and selected != "All":
        if conn.driver == "pg":
            mode_filter = " AND COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') = %s"
            params.append(selected)
        else:
            mode_filter = " AND COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') = ?"
            params.append(selected)
    date_clause, params = _append_date_filter(params)

    rows = conn.execute(f"""
        SELECT
            i.id,
            i.invoice_number,
            i.created_at,
            COALESCE(u.name, '') AS customer_name,
            COALESCE(SUM(ii.quantity),0) AS total_qty,
            COALESCE(SUM(ii.line_total),0) AS total_amount,
            COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') AS payment_mode
        FROM invoices i
        LEFT JOIN invoice_items ii ON ii.invoice_id = i.id
        LEFT JOIN users u ON u.id = i.user_id
        WHERE i.admin_id=%s {mode_filter} {date_clause}
        GROUP BY i.id, i.invoice_number, i.created_at, u.name, payment_mode
        ORDER BY i.created_at DESC
    """, tuple(params)).fetchall()

    # If filtered query returned nothing, build groups as in "All" so user sees other payment modes
    if not rows:
        groups = {}
        grand = {"orders": 0, "total_qty": 0, "total_amount": 0.0}
        for pm in payment_modes:
            p_params = [admin_id]
            if conn.driver == "pg":
                pm_filter = " AND COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') = %s"
                p_params.append(pm)
            else:
                pm_filter = " AND COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') = ?"
                p_params.append(pm)
            date_clause_pm, p_params = _append_date_filter(p_params)
            grp_rows = conn.execute(f"""
                SELECT
                    i.id,
                    i.invoice_number,
                    i.created_at,
                    COALESCE(u.name, '') AS customer_name,
                    COALESCE(SUM(ii.quantity),0) AS total_qty,
                    COALESCE(SUM(ii.line_total),0) AS total_amount,
                    COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') AS payment_mode
                FROM invoices i
                LEFT JOIN invoice_items ii ON ii.invoice_id = i.id
                LEFT JOIN users u ON u.id = i.user_id
                WHERE i.admin_id=%s {pm_filter} {date_clause_pm}
                GROUP BY i.id, i.invoice_number, i.created_at, u.name, payment_mode
                ORDER BY i.created_at DESC
            """, tuple(p_params)).fetchall()

            totals = {"orders": 0, "total_qty": 0, "total_amount": 0.0}
            for r in grp_rows:
                totals["orders"] += 1
                totals["total_qty"] += int(r["total_qty"] or 0)
                totals["total_amount"] += float(r["total_amount"] or 0.0)
            groups[pm] = {"rows": grp_rows, "totals": totals}
            grand["orders"] += totals["orders"]
            grand["total_qty"] += totals["total_qty"]
            grand["total_amount"] += totals["total_amount"]

        # render grouped view as fallback
        return render_template("analytics.html", payment_modes=payment_modes, selected=selected, groups=groups, grand=grand, selected_date=selected_date, fallback_groups=True)

    # Normal single-mode rendering (rows present)
    totals = {"orders": 0, "total_qty": 0, "total_amount": 0.0}
    for r in rows:
        totals["orders"] += 1
        totals["total_qty"] += int(r["total_qty"] or 0)
        totals["total_amount"] += float(r["total_amount"] or 0.0)

    return render_template("analytics.html", payment_modes=payment_modes, selected=selected, rows=rows, totals=totals, selected_date=selected_date)

if __name__ == "__main__":
  app.run(debug=True)
