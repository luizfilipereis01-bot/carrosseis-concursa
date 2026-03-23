"""
Vercel ASGI entry point.
Adds the backend directory to sys.path so all imports resolve correctly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from main import app  # noqa: F401  — Vercel detects the 'app' ASGI callable
