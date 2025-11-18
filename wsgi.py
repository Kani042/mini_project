# Simple WSGI entrypoint for Gunicorn / Render — import app from inventory_app
import sys
import os

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the Flask app from inventory_app package
from inventory_app.app import app
