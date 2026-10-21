"""The Mac service's real layout under a temporary home, as deploy/macos/install.sh creates it.

Every launchd test that runs under the rendered sandbox profile builds its service root through
`build`, so the root sits inside the home the profile is rendered with. The home's own name holds a
space and a double quote, so every rendered path goes through the profile's quoting.
"""

from pathlib import Path
from typing import NamedTuple


class Layout(NamedTuple):
    home: Path
    root: Path
    config: Path
    log: Path


def build(tmp_path: Path) -> Layout:
    """<home>/Library/Application Support/earshotd/{app,python,models,data,tmp}, the config
    directory <home>/.config/earshot and the log directory <home>/Library/Logs, all made."""
    home = tmp_path / 'my "home"'
    root = home / "Library" / "Application Support" / "earshotd"
    config = home / ".config" / "earshot" / "earshotd.toml"
    log = home / "Library" / "Logs" / "earshotd.log"
    root.mkdir(parents=True)
    for sub in ("app", "python", "models", "data", "tmp"):
        (root / sub).mkdir()
        (root / sub).chmod(0o700)
    config.parent.mkdir(parents=True)
    log.parent.mkdir(parents=True)
    return Layout(home, root, config, log)
