import importlib
import os

# Loads whichever bot file BOT_MODULE names (default: bot_v2).
app = importlib.import_module(os.environ.get("BOT_MODULE", "bot_v2")).app
