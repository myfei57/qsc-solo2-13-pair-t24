"""启动前自检（preflight）。

系统过去「起来就干」：量程、端口、命名空间是否配对全靠人记得，端口撞车一次要
查半天。自检在真正装配应用、绑定端口之前把四类问题挨个过一遍：

1. 配置——量程自洽性（``Settings.validate``）与命名空间格式，外加
   ``FLASHSMELTER_*`` 环境变量拼写检查（拼错的变量会被静默忽略，是最隐蔽的
   「我明明改了配置」来源）；
2. 数据目录——存在性与可创建性、写读删探针、落盘记录与审计流水完整性，以及
   同一根目录下是否混入其他命名空间的数据（状态串扰）；
3. 端口——实际尝试绑定，撞车、特权端口、地址不可用各自给出改法；
4. 依赖——Python 版本与平台各组件模块是否齐全，根目录所在盘剩余空间。

每个检查项要么通过，要么带着 ``fix``（该怎么改）返回；任何一项不过，
``serve`` 就拒绝启动，不让系统带病运行。自检只读/探针，不改动既有业务数据。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ENV_PREFIX, Settings
from .errors import FlashSmelterError
from .ns import Namespace
from .store import DurableStore
from .store.durable import DATA_DIR, JOURNAL_DIR

# 状态分级：error 阻断启动；warning 不阻断但必须让人看见；ok 只是留档。
SEVERITIES = ("ok", "warning", "error", "skipped")

REQUIRED_PYTHON = (3, 11)
MIN_FREE_BYTES = 16 * 1024 * 1024  # 状态文件很小，16 MiB 只用来挡住满盘/只读挂载
PROBE_FILENAME = ".preflight-probe"

# 平台内部组件模块；打包漏文件时这里会先报出来，而不是等运行到某个动作才炸。
COMPONENT_MODULES = (
    "flashsmelter.audit",
    "flashsmelter.burner",
    "flashsmelter.conc",
    "flashsmelter.config",
    "flashsmelter.console",
    "flashsmelter.conv",
    "flashsmelter.furnace",
    "flashsmelter.matte",
    "flashsmelter.oxygen",
    "flashsmelter.settler",
    "flashsmelter.slag",
    "flashsmelter.store",
    "flashsmelter.waste",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """单项自检结果。"""

    name: str
    group: str
    severity: str
    summary: str
    fix: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "severity": self.severity,
            "summary": self.summary,
            "fix": self.fix,
            "details": dict(self.details),
        }


def _ok(name: str, group: str, summary: str, **details: Any) -> CheckResult:
    return CheckResult(name, group, "ok", summary, details=details)


def _warning(name: str, group: str, summary: str, *, fix: str, **details: Any) -> CheckResult:
    return CheckResult(name, group, "warning", summary, fix=fix, details=details)


def _error(name: str, group: str, summary: str, *, fix: str, **details: Any) -> CheckResult:
    return CheckResult(name, group, "error", summary, fix=fix, details=details)


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """整份自检报告。``ok`` 才允许启动。"""

    results: tuple[CheckResult, ...]

    @property
    def errors(self) -> tuple[CheckResult, ...]:
        return tuple(item for item in self.results if item.severity == "error")

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(item for item in self.results if item.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def group(self, group: str) -> tuple[CheckResult, ...]:
        return tuple(item for item in self.results if item.group == group)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "results": [item.to_dict() for item in self.results],
        }

    def render_text(self) -> str:
        """渲染成值班人员一眼能看完的文本。"""

        labels = {"ok": "通过", "warning": "警告", "error": "失败", "skipped": "跳过"}
        titles = (
            ("config", "配置"),
            ("storage", "数据目录"),
            ("port", "端口"),
            ("dependency", "依赖"),
        )
        lines: list[str] = []
        for group, title in titles:
            lines.append(f"[{title}]")
            for item in self.group(group):
                lines.append(f"  {labels.get(item.severity, item.severity)}  {item.name}: {item.summary}")
                if item.fix:
                    lines.append(f"        改法：{item.fix}")
        if self.ok:
            tail = f"自检通过（{len(self.warnings)} 条警告），可以启动。"
        else:
            tail = f"自检未通过：{len(self.errors)} 项失败、{len(self.warnings)} 条警告，已阻止启动。"
        lines.append(tail)
        return "\n".join(lines)


# ------------------------------------------------------------------ 配置
def _check_config(
    settings: Settings, environ: Mapping[str, str]
) -> tuple[tuple[CheckResult, ...], Namespace | None]:
    results: list[CheckResult] = []

    # 量程与互相约束（富氧 min<max、渣层<液位、预算≥喷吹上限……）。
    try:
        settings.validate()
    except FlashSmelterError as exc:
        results.append(
            _error(
                "config.ranges",
                "config",
                f"工艺量程/配置自洽校验未过：{exc.message}",
                fix="按 details 中的字段改 --param 对应项或 FLASHSMELTER_* 环境变量后重试",
                **{"details": exc.details},
            )
        )
    else:
        results.append(_ok("config.ranges", "config", "工艺量程与互斥约束全部通过"))

    # 命名空间 site/unit。
    try:
        namespace = Namespace.parse(settings.namespace)
    except FlashSmelterError as exc:
        results.append(
            _error(
                "config.namespace",
                "config",
                f"命名空间不合法：{exc.message}",
                fix="用 --namespace site/unit 指定两级命名空间，每段为字母数字/_/-，例如 smelter/line1",
                value=settings.namespace,
            )
        )
        namespace = None
    else:
        results.append(
            _ok(
                "config.namespace",
                "config",
                f"命名空间为 {namespace.prefix}",
                namespace=namespace.as_dict(),
            )
        )
    # FLASHSMELTER_* 拼写检查：拼错的前缀变量会被静默忽略，是「改了不生效」的头号原因。
    known = {ENV_PREFIX + name.upper() for name in _config_field_names()} | {ENV_PREFIX + "ROOT"}
    unknown = sorted(
        name
        for name in environ
        if name.startswith(ENV_PREFIX) and name not in known and name != ENV_PREFIX.rstrip("_")
    )
    if unknown:
        results.append(
            _warning(
                "config.env_typos",
                "config",
                "存在不被识别的 FLASHSMELTER_* 变量（可能拼错，已被静默忽略）：" + ", ".join(unknown),
                fix="核对变量名拼写；支持的变量见配置字段（FLASHSMELTER_PORT / FLASHSMELTER_ROOT 等）",
                unknown=unknown,
            )
        )
    else:
        results.append(_ok("config.env_typos", "config", "FLASHSMELTER_* 环境变量无拼错项"))

    return tuple(results), namespace


def _config_field_names() -> tuple[str, ...]:
    return tuple(name for name in Settings.__slots__ if name != "root")  # type: ignore[attr-defined]


# ------------------------------------------------------------------ 数据目录
def _check_storage(settings: Settings, namespace: Namespace | None) -> tuple[CheckResult, ...]:
    results: list[CheckResult] = []
    root = Path(settings.root)

    # 目录存在性/可创建性。ensure_directories 是正常启动本来就会做的动作，自检阶段先做一次。
    try:
        settings.ensure_directories()
    except OSError as exc:
        results.append(
            _error(
                "storage.directories",
                "storage",
                f"状态目录无法创建：{root}（{exc.strerror or exc}）",
                fix="检查父目录权限与挂载状态，或用 --root 指向可写目录",
                root=str(root),
            )
        )
        # 目录都建不出来，后续检查没有意义。
        results.append(
            _error(
                "storage.write_probe",
                "storage",
                "因目录不可用而跳过写探针",
                fix="先修复状态目录",
            )
        )
        results.append(
            _error(
                "storage.integrity",
                "storage",
                "因目录不可用而跳过完整性校验",
                fix="先修复状态目录",
            )
        )
        return tuple(results)
    results.append(
        _ok(
            "storage.directories",
            "storage",
            f"状态目录可用：{root}",
            data_dir=str(root / DATA_DIR),
            journal_dir=str(root / JOURNAL_DIR),
        )
    )

    # 写/读/删探针：确认真的能落盘并回读，而不只是目录「看起来在」。
    probe = root / PROBE_FILENAME
    try:
        probe.write_bytes(b"flashsmelter-preflight")
        flushed = probe.read_bytes()
        if flushed != b"flashsmelter-preflight":
            raise OSError("回读内容与写入不一致")
        probe.unlink()
    except OSError as exc:
        results.append(
            _error(
                "storage.write_probe",
                "storage",
                f"状态目录写读探针失败：{exc.strerror or exc}",
                fix="检查目录权限、磁盘是否只读或已满；必要时更换 --root",
                root=str(root),
            )
        )
    else:
        results.append(_ok("storage.write_probe", "storage", "状态目录写入→回读→删除探针通过"))

    # 落盘完整性：记录校验和 + 审计流水序号。直接复用 store 的校验，不自造口径。
    try:
        store = DurableStore(root, clock=_UnusedClock())
        report = store.verify()
    except OSError as exc:
        results.append(
            _error(
                "storage.integrity",
                "storage",
                f"无法打开状态库：{exc.strerror or exc}",
                fix="检查状态目录权限；确认没有另一个进程独占",
                root=str(root),
            )
        )
    else:
        payload = report.to_dict()
        if report.ok:
            results.append(
                _ok(
                    "storage.integrity",
                    "storage",
                    (
                        f"落盘记录 {payload['records_checked']} 条、流水 {payload['journals_checked']} 路"
                        f"（{payload['journal_entries']} 行）校验通过"
                    ),
                    orphans_removed=payload["orphans_removed"],
                )
            )
        else:
            results.append(
                _error(
                    "storage.integrity",
                    "storage",
                    f"落盘数据校验发现 {len(report.problems)} 个问题：" + "；".join(report.problems[:3]),
                    fix="先跑 `flashsmelter verify` 查看全部问题并修复/隔离损坏文件，再启动",
                    problems=list(report.problems),
                )
            )

    # 命名空间串扰：同一根目录下若存在其他 site/unit 的数据，本进程会读不到却共享磁盘，
    # 通常是 --root 配错或两条产线指到了同一个根。
    if namespace is not None:
        results.append(_check_foreign_namespaces(root, namespace))

    # 磁盘剩余空间。
    results.append(_check_disk_space(root))
    return tuple(results)


def _check_foreign_namespaces(root: Path, namespace: Namespace) -> CheckResult:
    foreign: dict[str, int] = {}
    data_root = root / DATA_DIR
    if data_root.exists():
        for path in sorted(data_root.rglob("*.json")):
            key = "/".join(path.relative_to(data_root).with_suffix("").parts)
            parts = key.split("/")
            if len(parts) < 2:
                continue
            prefix = "/".join(parts[:2])
            if prefix != namespace.prefix:
                foreign[prefix] = foreign.get(prefix, 0) + 1

    # 审计流水的事件体里带 namespace 字段；发现别的命名空间事件同样提示。
    audit_path = root / JOURNAL_DIR / "audit" / "events.jsonl"
    foreign_events: dict[str, int] = {}
    if audit_path.exists():
        import json

        try:
            with audit_path.open("rb") as handle:
                for raw in handle:
                    text = raw.strip()
                    if not text:
                        continue
                    try:
                        parsed = json.loads(text.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue  # 损坏行交给 integrity 检查报
                    other = str((parsed.get("payload") or {}).get("namespace", ""))
                    if other and other != namespace.prefix:
                        foreign_events[other] = foreign_events.get(other, 0) + 1
        except OSError:
            pass

    if not foreign and not foreign_events:
        return _ok(
            "storage.namespace_isolation",
            "storage",
            f"根目录下只有本命名空间 {namespace.prefix} 的数据",
        )
    return _warning(
        "storage.namespace_isolation",
        "storage",
        "根目录中存在其他命名空间的数据："
        + ", ".join(f"{name}({count} 条)" for name, count in sorted({**foreign, **foreign_events}.items())),
        fix=(
            "确认 --root 是否指错：不同命名空间应使用各自独立的根目录；"
            f"当前进程只会读写 {namespace.prefix}，共享根目录存在误删/误判风险"
        ),
        foreign_records=foreign,
        foreign_audit_events=foreign_events,
    )


def _check_disk_space(root: Path) -> CheckResult:
    try:
        usage = shutil.disk_usage(str(root))
    except OSError as exc:
        return _warning(
            "storage.disk_space",
            "storage",
            f"无法读取磁盘剩余空间：{exc.strerror or exc}",
            fix="确认状态目录所在挂载点可用",
        )
    if usage.free < MIN_FREE_BYTES:
        return _error(
            "storage.disk_space",
            "storage",
            f"状态目录所在盘剩余空间不足：{usage.free // 1024} KiB",
            fix="清理磁盘或把 --root 指向其他挂载点后再启动",
            free_bytes=usage.free,
            min_bytes=MIN_FREE_BYTES,
        )
    return _ok(
        "storage.disk_space",
        "storage",
        f"状态目录所在盘剩余 {usage.free // (1024 * 1024)} MiB",
        free_bytes=usage.free,
    )


class _UnusedClock:  # DurableStore 只在写入路径用 clock；自检只 verify，不写入。
    def timestamp_iso(self) -> str:  # pragma: no cover - 自检路径不会调用
        raise RuntimeError("preflight 不应写入数据")


# ------------------------------------------------------------------ 端口
def _check_port(host: str, port: int) -> CheckResult:
    if not 1 <= port <= 65535:
        return _error(
            "port.bind",
            "port",
            f"监听端口超出范围：{port}",
            fix="用 --port 或 FLASHSMELTER_PORT 指定 1-65535 之间的端口",
            port=port,
        )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        holder = _who_holds(host, port)
        details: dict[str, Any] = {"host": host, "port": port, "reason": exc.strerror or str(exc)}
        if holder:
            details["hint"] = holder
        if exc.errno in (98, 10048):  # EADDRINUSE（Linux/Windows）
            summary = f"端口 {host}:{port} 已被占用"
            if holder:
                summary += f"（疑似 {holder}）"
            return _error(
                "port.bind",
                "port",
                summary,
                fix=(
                    "换用空闲端口（--port 或 FLASHSMELTER_PORT），或停掉占用进程后重试；"
                    "可用 `ss -ltnp` / `lsof -iTCP -sTCP:LISTEN` 查占用方"
                ),
                **details,
            )
        if exc.errno in (99, 10049):  # EADDRNOTAVAIL
            return _error(
                "port.bind",
                "port",
                f"本机没有地址 {host}，无法绑定",
                fix="确认网卡/IP 是否正确；只在本机访问可用默认 127.0.0.1，对外服务用 0.0.0.0",
                **details,
            )
        if exc.errno in (13, 10013) and port < 1024:
            return _error(
                "port.bind",
                "port",
                f"绑定特权端口 {port} 被拒绝（<1024 需要特权）",
                fix="改用 1024 以上端口，或以具备相应权限的方式启动",
                **details,
            )
        return _error(
            "port.bind",
            "port",
            f"端口 {host}:{port} 无法绑定：{exc.strerror or exc}",
            fix="检查监听地址、防火墙与端口占用情况后重试",
            **details,
        )
    finally:
        sock.close()

    note = "（<1024 为特权端口，需要特权才能绑定）" if port < 1024 else ""
    return _ok("port.bind", "port", f"端口 {host}:{port} 可绑定{note}".rstrip(), host=host, port=port)


def _who_holds(host: str, port: int) -> str | None:
    """尝试连一下端口；能连上说明确有进程在听，给自检报告多一条线索。"""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        if probe.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0:
            return f"有进程正在监听 {port}"
    except OSError:
        return None
    finally:
        probe.close()
    return None


# ------------------------------------------------------------------ 依赖
def _check_dependencies() -> tuple[CheckResult, ...]:
    results: list[CheckResult] = []

    # Python 版本。
    current = sys.version_info[:2]
    if current < REQUIRED_PYTHON:
        results.append(
            _error(
                "dependency.python",
                "dependency",
                f"Python 版本过低：{current[0]}.{current[1]}，要求 >={REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}",
                fix="换用 3.11 及以上解释器启动（检查 venv/systemd 的 Python 路径）",
                current="%d.%d" % current,
                required="%d.%d" % REQUIRED_PYTHON,
            )
        )
    else:
        results.append(
            _ok(
                "dependency.python",
                "dependency",
                f"Python {current[0]}.{current[1]} 满足 >={REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}",
            )
        )

    # 平台模块齐全性。平台本身零三方依赖，标准库在；这里主要防打包漏文件/装坏环境。
    missing = [name for name in COMPONENT_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        results.append(
            _error(
                "dependency.modules",
                "dependency",
                "平台模块缺失：" + ", ".join(missing),
                fix="重新安装 flashsmelter 包，或检查部署目录是否完整",
                missing=missing,
            )
        )
    else:
        results.append(_ok("dependency.modules", "dependency", f"平台 {len(COMPONENT_MODULES)} 个模块全部可导入"))

    return tuple(results)


# ------------------------------------------------------------------ 入口
def run_preflight(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    host: str | None = None,
    port: int | None = None,
) -> PreflightReport:
    """执行全部自检。``host``/``port`` 给 ``serve`` 的命令行覆盖值。

    配置项本身解析失败（例如环境变量不是数字）会以 ConfigurationError 抛出，
    由调用方当作阻断性启动错误处理；本函数只负责「能跑的检查都跑完」。
    """

    env = os.environ if environ is None else environ
    results: list[CheckResult] = []

    config_results, namespace = _check_config(settings, env)
    results.extend(config_results)

    results.extend(_check_storage(settings, namespace))

    listen_host = host if host is not None else settings.host
    listen_port = port if port is not None else settings.port
    results.append(_check_port(listen_host, listen_port))

    results.extend(_check_dependencies())

    return PreflightReport(tuple(results))


__all__ = [
    "CheckResult",
    "PreflightReport",
    "run_preflight",
    "report_from_boot_error",
    "SEVERITIES",
]


def report_from_boot_error(exc: FlashSmelterError) -> PreflightReport:
    """配置在装配阶段就抛错（如环境变量值无法解析）时，包装成一份单条失败报告。

    这样无论问题出在哪一层，值班人员看到的格式与改法提示都一致。
    """

    result = _error(
        "config.boot",
        "config",
        f"启动配置无法装配：{exc.message}",
        fix="按 details 中的字段核对命令行参数与 FLASHSMELTER_* 环境变量取值后重试",
        **{"details": exc.details},
    )
    return PreflightReport((result,))
