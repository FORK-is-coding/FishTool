"""在全新解释器内执行 launcher 的路径重定向，然后 import FishTool 核心模块并打印实际路径。

用法：python _redirect_probe.py <launcher.py> <engine_root> <data_root>
输出：一行 JSON。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def main() -> int:
    launcher_path, engine_root, data_root = (Path(p) for p in sys.argv[1:4])
    spec = importlib.util.spec_from_file_location("fishtool_launcher", launcher_path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    runtime_dir = launcher.apply_redirect(engine_root, data_root)

    from core.config import ConfigManager
    from core import request_budget, quota_store
    from core.database.api import init_database
    from core.logger import init_logger
    from modules.hotspot import risk_control

    cfg = ConfigManager()
    logger_manager = init_logger()
    init_database()
    import logging

    logging.getLogger("probe").error("probe error line")
    for handler in logging.getLogger().handlers:
        handler.flush()

    out = {
        "cwd": str(Path.cwd().resolve()),
        "runtime_dir": str(runtime_dir),
        "config_dir": str(Path(cfg.config_dir).resolve()),
        "key_path": str(Path(cfg.key_path).resolve()),
        "budget_request": str(request_budget._budget_config_path().resolve()),
        "budget_quota": str(quota_store._budget_config_path().resolve()),
        "budget_risk": str(risk_control._watch_scheduler_config_path().resolve()),
        "quota_limits_loaded": request_budget.load_quota_limits() is not None,
        "log_dir": str(Path(logger_manager.log_dir).resolve()),
    }
    print("PROBE_JSON=" + json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
