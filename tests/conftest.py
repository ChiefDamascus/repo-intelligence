"""Shared fixtures and pytest configuration."""
import os
import shutil
import subprocess
import sys

def pytest_configure(config):
    """Ensure the repo-index entry-point script is on PATH for subprocess tests."""
    # Locate where 'pip install -e . --user' placed the console_scripts entry point.
    user_bin = os.path.expanduser("~/.local/bin")
    if user_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = user_bin + os.pathsep + os.environ.get("PATH", "")

def pytest_runtest_setup(item):
    """Skip tests that require repo-index executable when the entry point is missing."""
    if "repo-index" in item.keywords or item.cls and any("repo-index" in str(m) for m in getattr(item, "iter_markers", lambda: [None])()):
        return
