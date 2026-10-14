"""The wealth package and the bank providers never load the agents feature (fork).

app/agents promises that nothing outside it is imported while AGENTS_ENABLED is
off. The agents' SQLAlchemy models are registered either way, so they are
allowed; services, runtime, API routers and the MCP server are not. Runs in a
fresh interpreter so modules other tests already imported do not hide a leak.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent

_PROBE = """
import json, sys
import app.main
import app.services.wealth.classify, app.services.wealth.coverage, app.services.wealth.merchant
import app.services.wealth.patterns, app.services.wealth.policy, app.services.wealth.spending
import app.services.wealth.buckets, app.services.wealth.figures, app.services.wealth.preferences
import app.services.wealth.projection, app.services.wealth.settings, app.services.wealth.valuation, app.api.wealth
import app.services.wealth.pace, app.services.wealth.budgets, app.services.wealth.levers
import app.providers.plaid, app.providers.simplefin, app.providers.enable_banking
allowed = ("app.agents", "app.agents.config", "app.agents.models")
leaked = sorted(
    m for m in sys.modules
    if (m.startswith("app.agents") and not (m in allowed or m.startswith("app.agents.models.")))
    or m.startswith("mcp_server")
)
print(json.dumps(leaked))
"""


def test_wealth_and_providers_load_without_the_agents_feature():
    env = {**os.environ, "AGENTS_ENABLED": "false"}
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=_BACKEND, env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    leaked = json.loads(out.stdout.strip().splitlines()[-1])
    assert leaked == [], f"imported with agents off: {leaked}"
