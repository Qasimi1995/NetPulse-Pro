import json
import os
import sys
import winreg
from typing import Any, Dict

SETTINGS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", "."), "NetPulse Pro"
)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "zoom_factor": 1.0,
    "autostart": False,
    "ping_host": "1.1.1.1",
    "quota_gb": 100.0,
    "quota_enabled": False,
    "graph_duration_seconds": 180,
    "reset_timestamp": "1970-01-01 00:00:00",
}


def load_settings() -> Dict[str, Any]:
  if os.path.exists(SETTINGS_FILE):
    try:
      with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        return {**DEFAULT_SETTINGS, **data}
    except Exception:
      pass
  return DEFAULT_SETTINGS.copy()


def save_settings(settings: Dict[str, Any]) -> None:
  try:
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
      json.dump(settings, f, indent=4)
  except Exception:
    pass


def set_windows_autostart(enabled: bool) -> bool:
  key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
  app_name = "NetPulsePro"
  exe_path = (
      sys.executable
      if getattr(sys, "frozen", False)
      else os.path.abspath(sys.argv[0])
  )

  try:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
    ) as key:
      if enabled:
        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'"{exe_path}"')
      else:
        try:
          winreg.DeleteValue(key, app_name)
        except FileNotFoundError:
          pass
    return True
  except Exception:
    return False
