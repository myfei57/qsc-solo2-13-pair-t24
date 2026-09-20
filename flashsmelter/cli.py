"""命令行入口。

CLI 与控制台共用同一份动作注册表：``call`` 子命令就是不带 HTTP 的动作调用，
方便值班人员在服务器上直接下发指令或做点检。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .application import Application
from .config import Settings
from .console import ConsoleApp, ConsoleServer
from .errors import FlashSmelterError, ValidationError
from .params import Params
from .preflight import Preflight, render_text


def _coerce(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _collect_params(raw_pairs: Sequence[str], raw_json: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if raw_json:
        try:
            decoded = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValidationError("--params-json 不是合法 JSON") from exc
        if not isinstance(decoded, dict):
            raise ValidationError("--params-json 必须是 JSON 对象")
        params.update({str(key): value for key, value in decoded.items()})
    for pair in raw_pairs:
        if "=" not in pair:
            raise ValidationError("--param 需要 key=value 形式", details={"value": pair})
        key, value = pair.split("=", 1)
        params[key.strip()] = _coerce(value)
    return params


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(None)
    root = getattr(args, "root", None)
    if root:
        settings = settings.with_root(Path(root))
    namespace = getattr(args, "namespace", None)
    if namespace:
        settings = replace(settings, namespace=namespace)
        settings.validate()
    return settings


def _load_settings(args: argparse.Namespace, *, host: str | None = None, port: int | None = None):
    """供自检与 serve 使用的宽容装载：配置再坏也不抛异常，交给自检报全。"""

    return Settings.load(
        None,
        root=getattr(args, "root", None),
        namespace=getattr(args, "namespace", None),
        host=host,
        port=port,
    )


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _run(action: str, params: Mapping[str, Any], args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    try:
        result = application.invoke(action, params, source=f"cli:{action}")
    except FlashSmelterError as exc:
        _print(exc.to_dict())
        return 1
    _print({"action": action, "result": dict(result)})
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # 起来就干、撞了再说的事故不能再出：绑定端口前先把自检全过一遍，
    # 任何一项未通过都不放行，问题与修改建议一次打全。
    settings, config_issues = _load_settings(args, host=args.host, port=args.port)
    preflight = Preflight(settings, config_issues=config_issues)
    report = preflight.run()
    if not report.ok:
        print(render_text(report), flush=True)
        preflight.release()
        return 3
    application = Application(settings)
    console = ConsoleApp(application)
    server = ConsoleServer(console, host=settings.host, port=settings.port)
    try:
        host, port = server.start()
    except OSError as exc:
        # 自检与真正绑定之间仍有极小竞态窗口：再兜一层，不打原始堆栈。
        print(
            f"[FAIL] 监听端口 —— {settings.host}:{settings.port} 绑定失败："
            f"{exc.strerror or exc}。请按 doctor 的「修改」建议处理后重试",
            flush=True,
        )
        preflight.release()
        return 3
    print(f"FlashSmelter 控制台已启动：http://{host}:{port}/api/state（Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        preflight.release()
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """启动前自检：配置/命名空间/数据目录/端口/依赖/单实例，不过不放行。"""

    settings, config_issues = _load_settings(args)
    preflight = Preflight(settings, config_issues=config_issues)
    report = preflight.run()
    if args.json:
        _print(report.to_dict())
    else:
        print(render_text(report), flush=True)
    preflight.release()
    return 0 if report.ok else 3


def _cmd_status(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    state = application.state()
    if getattr(args, "component", None):
        component = application.component(args.component)
        _print({"name": component.name, "status": dict(component.status()), "snapshot": dict(component.snapshot())})
        return 0
    _print(state)
    return 0


def _cmd_actions(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    _print({"actions": application.describe_actions()})
    return 0


def _cmd_call(args: argparse.Namespace) -> int:
    params = _collect_params(args.param or [], args.params_json)
    return _run(args.action, params, args)


def _cmd_audit(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    events = application.audit_events(
        limit=args.limit,
        since_seq=args.since,
        action=args.action,
        target=args.target,
        outcome=args.outcome,
        actor=args.actor,
    )
    _print({"count": len(events), "events": events})
    return 0


def _cmd_heat(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    _print(
        {
            "current": dict(application.furnace.heat_report()),
            "heats": [dict(item) for item in application.furnace.heats(limit=args.limit)],
            "converter": dict(application.conv.status()),
        }
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    report = application.verify()
    _print(report)
    return 0 if report.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flashsmelter",
        description="FlashSmelter 铜闪速熔炼炉精矿喷吹与放铜控制平台",
    )
    parser.add_argument("--root", help="状态根目录（默认 var/）")
    parser.add_argument("--namespace", help="冶炼命名空间 site/unit")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动前自检通过后启动 JSON 控制台")
    serve.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，可用 FLASHSMELTER_HOST）")
    serve.add_argument("--port", type=int, default=None, help="监听端口（默认 8080，可用 FLASHSMELTER_PORT）")
    serve.set_defaults(func=_cmd_serve)

    status = subparsers.add_parser("status", help="打印平台状态")
    status.add_argument("--component", help="只打印指定组件")
    status.set_defaults(func=_cmd_status)

    actions = subparsers.add_parser("actions", help="列出可用动作")
    actions.set_defaults(func=_cmd_actions)

    call = subparsers.add_parser("call", help="下发一条控制指令")
    call.add_argument("action", help="动作名，如 furnace.start")
    call.add_argument("--param", action="append", help="key=value，可重复")
    call.add_argument("--params-json", help="以 JSON 对象形式给出参数")
    call.set_defaults(func=_cmd_call)

    audit = subparsers.add_parser("audit", help="查询审计流水")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--since", type=int, default=0)
    audit.add_argument("--action")
    audit.add_argument("--target")
    audit.add_argument("--outcome")
    audit.add_argument("--actor")
    audit.set_defaults(func=_cmd_audit)

    heat = subparsers.add_parser("heat", help="打印炉次与转炉批次")
    heat.add_argument("--limit", type=int, default=5)
    heat.set_defaults(func=_cmd_heat)

    verify = subparsers.add_parser("verify", help="校验落盘数据完整性")
    verify.set_defaults(func=_cmd_verify)

    doctor = subparsers.add_parser("doctor", help="启动前自检：配置、数据目录、端口、依赖、单实例逐项过")
    doctor.add_argument("--json", action="store_true", help="以 JSON 输出自检结果")
    doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    if getattr(args, "version", False):
        from . import __version__

        print(__version__)
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args))
    except FlashSmelterError as exc:
        _print(exc.to_dict())
        return 1


__all__ = ["main", "build_parser"]
