# Gunicorn config for WarRoom. Eventlet worker for Flask-SocketIO (real-time draft).
# Run: gunicorn -c gunicorn.conf.py app:app
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5053')}"
workers = 1                 # Socket.IO + in-process room state require a single worker
worker_class = "eventlet"
timeout = 120
loglevel = "info"
