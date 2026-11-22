"""Isolate tests from the real DB / keys. Must run before `app` is imported."""

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="fxnews-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["OPENROUTER_API_KEY"] = "test-key"
# Never read the developer's real backend/llm.yaml (it holds keys): point at a file that does not exist.
# tests/test_llm_yaml_config.py writes its own files and passes them explicitly.
os.environ["LLM_CONFIG"] = str(_tmp / "no-llm.yaml")
os.environ["DISCORD_WEBHOOK_URL"] = ""
os.environ["TWELVEDATA_API_KEY"] = ""
# Starlette's TestClient sends `Host: testserver`; the production default allow-list is localhost/127.0.0.1.
os.environ["ALLOWED_HOSTS"] = '["localhost", "127.0.0.1", "testserver"]'
os.environ["TRADINGVIEW_ENABLED"] = "false"  # tests never open the real websocket; test_tradingview.py opts in
