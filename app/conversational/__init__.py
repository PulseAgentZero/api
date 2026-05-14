"""Conversational HTTP surface: shared `/api/v1/agent` router + standalone ASGI app.

This package is intentionally **not** named ``agent`` — that collides mentally with
``app.agents`` (batch pipeline: orchestrator, workflows, tools).
"""
