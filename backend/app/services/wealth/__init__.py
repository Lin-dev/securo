"""Wealth analytics shared by the API, the dashboard and the agents (fork).

Pure figures and policy over the ledger. Nothing here imports app.agents or
mcp_server, so API code can use it with the agents feature switched off;
tests/test_wealth_isolation.py holds that line.
"""
