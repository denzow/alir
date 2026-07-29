"""設定: データディレクトリの解決。"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "ALIR_DATA_DIR"


def data_dir() -> Path:
    """iceql データディレクトリのパスを返す。

    環境変数 ALIR_DATA_DIR が指すパスを使う。
    未指定なら XDG_DATA_HOME/alir(既定は ~/.local/share/alir)。
    """
    if env := os.environ.get(ENV_DATA_DIR):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME", "~/.local/share")
    return Path(xdg).expanduser() / "alir"
