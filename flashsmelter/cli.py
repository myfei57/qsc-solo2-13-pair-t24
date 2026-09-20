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
from .preflight import report_from_boot_error, run_preflight


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


def _resolve_listen_settings(args: argparse.Namespace) -> Settings:
    """合并根/命名空间/监听参数，得到 serve 与 doctor 共用的最终配置。"""

    settings = replace(_build_settings(args), host=args.host, port=args.port)
    settings.validate()
    return settings


def _preflight(settings: Settings, *, as_json: bool) -> int | None:
    """跑启动自检；未通过时打印报告并返回退出码，通过返回 None。"""

    report = run_preflight(settings, host=settings.host, port=settings.port)
    if as_json:
        _print(report.to_dict())
    else:
        print(report.render_text(), flush=True)
    if not report.ok:
        return 3
    return None


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        settings = _resolve_listen_settings(args)
    except FlashSmelterError as exc:
        if args.skip_preflight:
            raise
        print(report_from_boot_error(exc).render_text(), file=sys.stderr, flush=True)
        return 3
    if not args.skip_preflight:
        failed = _preflight(settings, as_json=False)
        if failed is not None:
            return failed
    else:
        print("已按 --skip-preflight 跳过启动自检（不建议），直接启动。", flush=True)
    application = Application(settings)
    console = ConsoleApp(application)
    server = ConsoleServer(console, host=settings.host, port=settings.port)

    # 先绑定端口再宣布启动；绑定失败（自检之后被人抢端口等）给出一致的改法提示。
    try:
        host, port = server.bind()
    except OSError as exc:
        print(
            f"控制台绑定 {settings.host}:{settings.port} 失败：{exc.strerror or exc}。"
            "可先运行 `flashsmelter doctor` 定位问题。",
            file=sys.stderr,
            flush=True,
        )
        return 3
    print(f"FlashSmelter 控制台已启动：http://{host}:{port}/api/state（Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


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


def _cmd_doctor(args: argparse.Namespace) -> int:
    try:
        settings = _resolve_listen_settings(args)
    except FlashSmelterError as exc:
        report = report_from_boot_error(exc)
    else:
        report = run_preflight(settings, host=settings.host, port=settings.port)
    if args.json:
        _print(report.to_dict())
    else:
        print(report.render_text(), flush=True)
    return 3 if not report.ok else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flashsmelter",
        description="FlashSmelter 铜闪速熔炼炉精矿喷吹与放铜控制平台",
    )
    parser.add_argument("--root", help="状态根目录（默认 var/）")
    parser.add_argument("--namespace", help="冶炼命名空间 site/unit")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动 JSON 控制台（启动前自动自检）")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--skip-preflight", action="store_true", help="跳过启动自检（不建议）")
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

    doctor = subparsers.add_parser("doctor", help="启动前自检：配置、数据目录、端口、依赖")
    doctor.add_argument("--host", default="127.0.0.1", help="拟监听地址，与 serve 保持一致")
    doctor.add_argument("--port", type=int, default=8080, help="拟监听端口，与 serve 保持一致")
    doctor.add_argument("--json", action="store_true", help="输出 JSON 报告（默认输出可读文本）")
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
