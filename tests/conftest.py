"""Allow the flat AstrBot plugin package to run its tests from a clean clone."""
from __future__ import annotations
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if "astrbot_plugin_cove_companion" not in sys.modules:
    package = types.ModuleType("astrbot_plugin_cove_companion")
    package.__path__ = [str(ROOT)]
    package.__package__ = "astrbot_plugin_cove_companion"
    sys.modules["astrbot_plugin_cove_companion"] = package
