# Simple WSGI entrypoint for Gunicorn / Render — import app from inventory_app
try:
    # inventory_app exposes `app` at module level (inventory_app/app.py creates `app`)
    from inventory_app.app import app
except Exception:
    # fallback: if your other package uses create_app(), try that
    try:
        from app import create_app
        app = create_app()
    except Exception:
        raise
