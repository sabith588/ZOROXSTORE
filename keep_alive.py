from flask import Flask
from threading import Thread
import os
import logging

# Disable Flask logging to keep your console clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

@app.route('/')
def home():
    return "ZoroX Bot is Online and Running!"

def run():
    # Render assigns a dynamic PORT environment variable. We must bind to it.
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
