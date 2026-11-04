"""在真实 AstrBot 环境里加载插件 main.py，检查聊天指令是否注册成功、参数类型是否符合预期。

不启动引擎、不连平台，只验证装饰器注册结果和 handler 签名。需要装了 AstrBot 的 Python（>=3.12）：

    <astrbot-python> integrations/astrbot/harness/smoke_commands.py

成功时退出码 0 并打印指令表；任何检查失败退出码 1。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"
PKG = "fishtool_plugin_smoke"

#: 期望注册的子指令 → 期望的参数（名字: 默认值或注解）。
EXPECTED = {
    "状态": {},
    "热点": {"limit": "10"},
    "告警": {"limit": "5", "level": "all"},
    "舆情": {"bvid": "", "strategy": "normal"},
    "监控": {"action": "状态", "bvids": ""},
    "订阅": {},
    "退订": {},
}


def load_main():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG] = pkg
    spec = importlib.util.spec_from_file_location(f"{PKG}.main", PLUGIN_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    module = load_main()
    handlers = star_handlers_registry.get_handlers_by_module_name(module.__name__)
    groups = [f for h in handlers for f in h.event_filters if isinstance(f, CommandGroupFilter)]
    errors: list[str] = []
    if len(groups) != 1:
        errors.append(f"expected 1 command group, got {len(groups)}")
        print("\n".join(errors))
        return 1
    group = groups[0]
    names = group.get_complete_command_names()
    if set(names) != {"fishtool", "ft"}:
        errors.append(f"group names {names}")

    subs = {}
    for sub in group.sub_command_filters:
        if isinstance(sub, CommandFilter):
            subs[sub.command_name] = sub
    if set(subs) != set(EXPECTED):
        errors.append(f"subcommands {sorted(subs)} != {sorted(EXPECTED)}")

    for name, expect in EXPECTED.items():
        sub = subs.get(name)
        if sub is None:
            continue
        if sub.handler_params != expect:
            errors.append(f"{name}: params {sub.handler_params} != {expect}")
        md = sub.get_handler_md()
        if not md.extras_configs.get("sub_command"):
            errors.append(f"{name}: not marked as sub_command")
        if not (md.desc or "").strip():
            errors.append(f"{name}: missing description")
        print(f"/fishtool {name}  params={sub.handler_params}  desc={md.desc}")

    # 解析参数：模拟 "/fishtool 告警 3 high" 与输错的 "/fishtool 告警 abc"；都不能抛异常
    alerts = subs.get("告警")
    if alerts is not None:
        for args, expect in ((["3", "high"], {"limit": "3", "level": "high"}),
                             (["abc"], {"limit": "abc", "level": "all"}),
                             ([], {"limit": "5", "level": "all"})):
            try:
                parsed = alerts.validate_and_convert_params(args, alerts.handler_params)
            except ValueError as exc:
                errors.append(f"告警 {args} raised {exc}")
                continue
            if parsed != expect:
                errors.append(f"告警 {args} parsed {parsed}")

    if errors:
        print("FAILED:\n  " + "\n  ".join(errors))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
