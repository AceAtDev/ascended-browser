"""Standalone stand-ins for the Ascended app modules the browser code uses.

Generated code in ``_app`` imports these instead of the app. Each module keeps
the app's names and call shapes; features that need the full app (the desktop
browser host, the login vault, sub-agents, LLM-backed extraction) report that
they are unavailable instead of failing.
"""
