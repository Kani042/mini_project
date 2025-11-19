import os, uuid, sys
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
from passlib.hash import pbkdf2_sha256
from dotenv import load_dotenv
from .db import get_db, init_db

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

# -------- Helpers --------
def require_admin():
  if not session.get("admin_id"):
    flash("Please log in as admin.")
    return False
  return True

def current_stock(conn, item_id):
  cur = conn.execute("SELECT COALESCE(SUM(delta_quantity),0) AS qty FROM inventory_stock WHERE item_id=%s", (item_id,))
  return cur.fetchone()["qty"]

# -------- Auth --------
@app.route("/register", methods=["GET", "POST"])
def register():
  conn = get_db()
  if request.method == "POST":
    email = request.form["email"].lower().strip()
    password = request.form["password"]
    if not email or not password:
      flash("Email and password required.")
      return redirect(url_for("register"))
    pw_hash = pbkdf2_sha256.hash(password)
    try:
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
    admin = conn.execute("SELECT * FROM admins WHERE email=%s", (email,)).fetchone()
    if not admin:
      print(f"No admin found with email: {email}")
      flash("Invalid credentials.")
    elif pbkdf2_sha256.verify(password, admin["password_hash"]):
      print(f"Login successful for: {email}")
      session["admin_id"] = admin["id"]
      session["admin_email"] = admin["email"]
      return redirect(url_for("dashboard"))
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
            # ignore if DB refuses (concurrency or Postgres restrictions) — we'll fail later with clear error
            pass
      except Exception:
        # best-effort; continue
        pass

      # Create invoice (do not commit yet)
      conn.execute(
        "INSERT INTO invoices (invoice_number, user_id, subtotal, tax, total, admin_id, payment_mode) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (invoice_number, user["id"], subtotal, tax, total, admin_id, payment_mode)
      )
      invoice = conn.execute("SELECT * FROM invoices WHERE invoice_number=%s", (invoice_number,)).fetchone()

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

@app.route("/checkout/search", methods=["GET"])
def checkout_search():
  if not require_admin():
    return jsonify({"error": "Unauthorized"}), 401
  
  query = request.args.get("q", "").strip()
  admin_id = session.get("admin_id")
  
  print(f"\n--- Search Request ---")
  print(f"Query: '{query}'")
  print(f"Admin ID: {admin_id}")
  print(f"Query length: {len(query)}")
  
  if not query or len(query) < 3:
    print(f"Query too short (min 3 chars required)")
    return jsonify([])
  
  conn = get_db()
  
  try:
    # First check if admin has any items
    admin_items_count = conn.execute(
      "SELECT COUNT(*) as cnt FROM inventory_items WHERE admin_id=%s", 
      (admin_id,)
    ).fetchone()["cnt"]
    print(f"Admin has {admin_items_count} total items")
    
    # Search by SKU or name - only this admin's items
    items = conn.execute("""
      SELECT id, sku, name, unit_price 
      FROM inventory_items 
      WHERE admin_id=%s AND (sku LIKE %s OR name LIKE %s)
      ORDER BY name
    """, (admin_id, f"%{query}%", f"%{query}%")).fetchall()
    
    print(f"Search found {len(items)} matching items")
    
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
      print(f"  - {item['sku']}: {item['name']} (stock: {stock})")
    
    print(f"✓ Search successful: {len(result)} results\n")
    return jsonify(result)
  except Exception as e:
    print(f"✗ Search error: {e}\n")
    import traceback
    traceback.print_exc()
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
	"""Show pivoted sales by payment mode and list orders for selected mode."""
	if not require_admin():
		return redirect(url_for("login"))
	conn = get_db()
	admin_id = session.get("admin_id")

	# get available payment modes for this admin
	modes_rows = conn.execute(
		"SELECT DISTINCT COALESCE(NULLIF(payment_mode, ''), 'Unknown') AS payment_mode FROM invoices WHERE admin_id=%s ORDER BY payment_mode",
		(admin_id,)
	).fetchall()
	payment_modes = [r["payment_mode"] for r in modes_rows] if modes_rows else []

	# selected mode from query string; 'All' means no filter
	selected = request.args.get("payment_mode", "All")

	# Build invoice list query, filter by payment_mode if selected != All
	params = [admin_id]
	mode_filter = ""
	if selected and selected != "All":
		mode_filter = " AND COALESCE(NULLIF(i.payment_mode, ''), 'Unknown') = %s"
		params.append(selected)

	# Query invoices with aggregated qty & amount per invoice
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
		WHERE i.admin_id=%s {mode_filter}
		GROUP BY i.id, i.invoice_number, i.created_at, u.name, payment_mode
		ORDER BY i.created_at DESC
	""", tuple(params)).fetchall()

	# Consolidated totals for the displayed rows
	totals = {"orders": 0, "total_qty": 0, "total_amount": 0.0}
	for r in rows:
		totals["orders"] += 1
		totals["total_qty"] += int(r["total_qty"] or 0)
		totals["total_amount"] += float(r["total_amount"] or 0.0)

	return render_template("analytics.html", payment_modes=payment_modes, selected=selected, rows=rows, totals=totals)

if __name__ == "__main__":
  app.run(debug=True)
