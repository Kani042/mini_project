import sqlite3
import os

# Ensure DB_PATH is in the app directory
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")

def get_db():
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  return conn

def init_db():
  """Initialize database with all required tables"""
  conn = get_db()
  cur = conn.cursor()
  
  try:
    # Create admins table
    cur.execute("""
      CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    """)
    
    # Create users table
    cur.execute("""
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile TEXT UNIQUE NOT NULL,
        name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    """)
    
    # Create inventory_items table
    cur.execute("""
      CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        sku TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        unit_price REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (admin_id) REFERENCES admins(id),
        UNIQUE(admin_id, sku)
      )
    """)
    
    # Create inventory_stock table
    cur.execute("""
      CREATE TABLE IF NOT EXISTS inventory_stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        delta_quantity INTEGER NOT NULL,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (item_id) REFERENCES inventory_items(id)
      )
    """)
    
    # Create invoices table
    cur.execute("""
      CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        invoice_number TEXT UNIQUE NOT NULL,
        user_id INTEGER NOT NULL,
        subtotal REAL NOT NULL,
        tax REAL NOT NULL,
        total REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (admin_id) REFERENCES admins(id)
      )
    """)
    
    # Create invoice_items table
    cur.execute("""
      CREATE TABLE IF NOT EXISTS invoice_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        line_total REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id),
        FOREIGN KEY (item_id) REFERENCES inventory_items(id)
      )
    """)
    
    conn.commit()
    print(f"Database path: {DB_PATH}")
    
  except Exception as e:
    print(f"Error creating tables: {e}")
    raise
  finally:
    conn.close()

  print("✓ Database initialized successfully!")
