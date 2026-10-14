"""
tests.fixtures - Financial Statement Test Fixtures Registry

Provides absolute path resolution helper for test fixture assets (XML, PDF, etc.).
"""

import os

FIXTURES_DIR = os.path.dirname(os.path.abspath(__file__))

def get_fixture_path(filename: str) -> str:
    """Returns the absolute path to a fixture file within tests/fixtures/."""
    path = os.path.join(FIXTURES_DIR, filename)
    if not os.path.exists(path):
        # Fallback to project root if needed
        root_path = os.path.join(os.path.dirname(os.path.dirname(FIXTURES_DIR)), filename)
        if os.path.exists(root_path):
            return root_path
        raise FileNotFoundError(f"Fixture no encontrada: {filename} en {path}")
    return path
