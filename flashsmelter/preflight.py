"""启动前自检（preflight）。

产线上过一次「端口撞车查了半天」的事故，根因是系统起来就干：量程、端口、
命名空间全靠人记，配置不自洽、数据目录串了炉、端口已被占用都要等到运行时才
暴露。自检在真正绑定端口、装配组件之前把这些项挨个过一遍：

1. ``config``     环境变量可解析、工艺量程自洽、命名空间格式合法；
2. ``namespace``  数据目录里的落盘前缀与配置命名空间是否配对；
3. ``data``       数据目录存在/可建/可写，落盘数据校验和完整，磁盘有余量；
4. ``port``       端口在范围内且能真的绑定，占用时给出占用方；
5. ``dependencies`` Python 版本与运行所需模块齐备；
6. ``instance``   同一状态根上没有另一个实例在跑（排他运行锁）。

原则：**发现问题不抛异常、不中途停**，把每一项的问题、定位细节和修改建议
一次收集全；只要存在 error 级问题，调用方（``serve``）就必须拒绝启动。
"""

from __future__ import annotations

import errno
import importlib
import json
import os
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .config import ENV_PREFIX, ConfigIssue, Settings
from .errors import FlashSmelterError
from .ns import Namespace
from .runtime import Clock
from .store.durable import DATA_DIR, JOURNAL_DIR, DurableStore

# 检查项固定顺序，报告与 JSON 都按此输出。
CHECK_ORDER: tuple[str, ...] = ("config", "namespace", "data", "port", "dependencies", "instance")
CHECK_TITLES: Mapping[str, str] = {
    "config": "配置与工艺量程",
    "namespace": "命名空间配对",
    "data": "数据目录与落盘完整性",
    "port": "监听端口",
    "dependencies": "运行依赖",
    "instance": "单实例排他",
}

OK = "ok"
WARN = "warn"
ERROR = "error"
SKIPPED = "skipped"

# 磁盘余量阈值：低于下限直接拒绝启动，低于上限给出告警。
FREE_BYTES_HARD = 100 * 1024 * 1024
FREE_BYTES_SOFT = 1024 * 1024 * 1024

# (模块, 是否必需, 用途)。fcntl 只用于 POSIX 运行锁，非 POSIX 平台告警跳过。
_REQUIRED_MODULES: tuple[tuple[str, bool, str], ...] = (
    ("http.server", True, "JSON 控制台"),
    ("json", True, "落盘与接口序列化"),
    ("socket", True, "端口绑定探测"),
    ("threading", True, "控制台工作线程"),
    ("hashlib", True, "落盘校验和"),
    ("fcntl", os.name == "posix", "单实例排他运行锁"),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """一条自检发现：级别、现象、定位细节、修改建议。"""

    level: str
    message: str
    fix: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"level": self.level, "message": self.message}
        if self.fix:
            payload["fix"] = self.fix
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(slots=True)
class CheckResult:
    id: str
    title: str
    findings: list[Finding] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def level_counts(self) -> Mapping[str, int]:
        counts = {OK: 0, WARN: 0, ERROR: 0}
        for finding in self.findings:
            counts[finding.level] = counts.get(finding.level, 0) + 1
        return counts

    @property
    def status(self) -> str:
        if self.skipped_reason:
            return SKIPPED
        counts = self.level_counts
        if counts[ERROR]:
            return ERROR
        if counts[WARN]:
            return WARN
        return OK

    @property
    def summary(self) -> str:
        if self.skipped_reason:
            return self.skipped_reason
        counts = self.level_counts
        parts = []
        if counts[ERROR]:
            parts.append(f"{counts[ERROR]} 个问题")
        if counts[WARN]:
            parts.append(f"{counts[WARN]} 个告警")
        return "，".join(parts) if parts else "通过"

    def add(self, level: str, message: str, *, fix: str = "", **details: Any) -> None:
        self.findings.append(Finding(level=level, message=message, fix=fix, details=dict(details)))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
        }
        if self.skipped_reason:
            payload["skipped_reason"] = self.skipped_reason
        return payload


@dataclass(slots=True)
class PreflightReport:
    checks: list[CheckResult]

    @property
    def ok(self) -> bool:
        return all(check.status != ERROR for check in self.checks)

    @property
    def problems(self) -> int:
        return sum(check.level_counts[ERROR] for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.level_counts[WARN] for check in self.checks)

    def get(self, check_id: str) -> CheckResult:
        for check in self.checks:
            if check.id == check_id:
                return check
        raise KeyError(check_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": self.problems,
            "warnings": self.warnings,
            "checks": [check.to_dict() for check in self.checks],
        }


class InstanceLock:
    """状态根上的排他运行锁。

    用 POSIX ``flock`` 锁住 ``<root>/.runtime.lock``：进程退出（含崩溃）时
    内核自动释放，不会留下误判的「僵尸锁」；锁文件里写入持有者信息，启动
    冲突时可以直接告诉值班人员是谁占着。
    """

    LOCK_NAME = ".runtime.lock"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / self.LOCK_NAME
        self._handle: Any = None

    def acquire(self, metadata: Mapping[str, Any]) -> None:
        import fcntl  # POSIX 专属；依赖检查在非 POSIX 平台会告警。

        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        self._handle = handle

    def read_holder(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class Preflight:
    """按固定顺序执行全部启动前检查并汇总报告。"""

    def __init__(self, settings: Settings, *, config_issues: Sequence[ConfigIssue] = ()) -> None:
        self.settings = settings
        self._config_issues = list(config_issues)
        self._checks = {
            check_id: CheckResult(id=check_id, title=CHECK_TITLES[check_id]) for check_id in CHECK_ORDER
        }
        self.clock = Clock()
        self.lock = InstanceLock(settings.root)

    # ------------------------------------------------------------------ 入口
    def run(self) -> PreflightReport:
        namespace = self._check_config()
        self._check_dependencies()
        data_ready = self._check_data()
        if data_ready:
            self._check_namespace(namespace)
        else:
            self._checks["namespace"].skipped_reason = "数据目录不可用，无法核对命名空间"
        if 1 <= self.settings.port <= 65535:
            self._check_port()
        else:
            self._checks["port"].skipped_reason = "端口超出范围，见配置检查"
        self._check_instance(namespace)
        return PreflightReport(checks=[self._checks[check_id] for check_id in CHECK_ORDER])

    # ------------------------------------------------------------- 1. 配置
    def _check_config(self) -> Namespace | None:
        check = self._checks["config"]
        for issue in self._config_issues:
            check.add(ERROR, issue.message, fix=issue.fix, **issue.details)
        namespace: Namespace | None = None
        try:
            namespace = Namespace.parse(self.settings.namespace)
        except FlashSmelterError as exc:
            check.add(
                ERROR,
                exc.message,
                fix=f"命名空间必须形如 site/unit：检查 {ENV_PREFIX}NAMESPACE 或 --namespace",
                **exc.details,
            )
        if not check.findings:
            check.add(OK, "环境变量可解析，工艺量程自洽")
        return namespace

    # ------------------------------------------------------------- 2. 依赖
    def _check_dependencies(self) -> None:
        check = self._checks["dependencies"]
        if sys.version_info < (3, 11):
            check.add(
                ERROR,
                f"Python 版本过低：当前 {sys.version.split()[0]}，平台要求 >= 3.11",
                fix="换用 Python 3.11 及以上解释器启动",
                current=sys.version.split()[0],
                required=">=3.11",
            )
        else:
            check.add(OK, f"Python {sys.version.split()[0]} 满足 >= 3.11")
        for module_name, required, purpose in _REQUIRED_MODULES:
            try:
                importlib.import_module(module_name)
            except ImportError:
                level = ERROR if required else WARN
                check.add(
                    level,
                    f"缺少运行所需模块 {module_name!r}（{purpose}）",
                    fix="安装提供该模块的运行时；标准库模块缺失说明解释器安装不完整",
                    module=module_name,
                    purpose=purpose,
                )
            else:
                check.add(OK, f"模块 {module_name} 可用（{purpose}）")

    # ------------------------------------------------------------- 3. 数据目录
    def _check_data(self) -> bool:
        check = self._checks["data"]
        settings = self.settings
        root = Path(settings.root)
        for name, path in (("状态根目录", root), ("数据目录", root / DATA_DIR), ("流水目录", root / JOURNAL_DIR)):
            if path.exists() and not path.is_dir():
                check.add(
                    ERROR,
                    f"{name}路径存在但不是目录：{path}",
                    fix="移走或删除该文件，或用 --root 指定专用状态目录",
                    path=str(path),
                )
                return False
        try:
            resolved = root.resolve(strict=False)
            root.mkdir(parents=True, exist_ok=True)
            (root / DATA_DIR).mkdir(parents=True, exist_ok=True)
            (root / JOURNAL_DIR).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            check.add(
                ERROR,
                f"数据目录无法创建：{exc.strerror or exc}",
                fix="检查父目录权限与磁盘状态，或用 --root 指定可写目录",
                root=str(root),
                errno=exc.errno,
            )
            return False
        if str(resolved) != str(root):
            check.add(OK, f"状态根目录解析为 {resolved}")
        data_writable = self._probe_writable(check, root / DATA_DIR)
        journal_writable = self._probe_writable(check, root / JOURNAL_DIR)
        if not data_writable or not journal_writable:
            return False
        self._check_free_space(check, resolved)
        self._check_integrity(check, resolved)
        if not check.findings:
            check.add(OK, "数据目录可写，落盘数据完整")
        return check.status != ERROR

    @staticmethod
    def _probe_writable(check: CheckResult, directory: Path) -> bool:
        probe = directory / ".preflight-probe.tmp"
        try:
            probe.write_bytes(b"preflight")
            probe.unlink()
        except OSError as exc:
            check.add(
                ERROR,
                f"目录不可写：{directory}（{exc.strerror or exc}）",
                fix="修正属主与权限（平台需要读写该目录），或换用专用 --root",
                path=str(directory),
                errno=exc.errno,
            )
            return False
        return True

    def _check_free_space(self, check: CheckResult, path: Path) -> None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return
        free_mib = usage.free // (1024 * 1024)
        if usage.free < FREE_BYTES_HARD:
            check.add(
                ERROR,
                f"磁盘剩余空间仅 {free_mib} MiB，不足以安全运行",
                fix="清理磁盘或更换状态根目录后再启动",
                free_bytes=usage.free,
            )
        elif usage.free < FREE_BYTES_SOFT:
            check.add(
                WARN,
                f"磁盘剩余空间偏低：{free_mib} MiB",
                fix="建议尽快清理，避免流水写满后平台被迫拒动作",
                free_bytes=usage.free,
            )

    def _check_integrity(self, check: CheckResult, root: Path) -> None:
        try:
            store = DurableStore(root, clock=self.clock)
            report = store.verify()
        except FlashSmelterError as exc:
            check.add(
                ERROR,
                f"落盘库无法打开：{exc.message}",
                fix="按 details 中的定位修复，或从备份恢复状态目录",
                **exc.details,
            )
            return
        if store.orphans_removed:
            check.add(WARN, f"清理了 {store.orphans_removed} 个上次崩溃残留的临时文件")
        for problem in report.problems:
            check.add(
                ERROR,
                f"落盘数据损坏：{problem}",
                fix="修复或隔离该记录后再启动；带病启动会基于错误状态下发指令",
            )
        if not report.problems:
            check.add(
                OK,
                f"整库校验通过：{report.records_checked} 份文档、{report.journal_entries} 条流水",
            )

    # ------------------------------------------------------------- 4. 命名空间
    def _check_namespace(self, namespace: Namespace | None) -> None:
        check = self._checks["namespace"]
        if namespace is None:
            check.skipped_reason = "命名空间不合法，见配置检查"
            return
        data_root = Path(self.settings.root) / DATA_DIR
        prefixes = self._scan_namespace_prefixes(data_root)
        current = namespace.prefix
        if current in prefixes:
            check.add(
                OK,
                f"落盘数据属于 {current}",
                records=prefixes[current],
            )
            for other in sorted(prefixes):
                if other != current:
                    check.add(
                        WARN,
                        f"同一状态根下还存在另一命名空间 {other} 的 {prefixes[other]} 份数据",
                        fix="多炉并行请各自使用独立 --root，避免共目录运维时误操作",
                        other_namespace=other,
                    )
        elif prefixes:
            check.add(
                ERROR,
                f"配置命名空间 {current} 与数据目录不配对：目录里是 {', '.join(sorted(prefixes))} 的数据",
                fix=(
                    f"改用匹配的命名空间（{ENV_PREFIX}NAMESPACE 或 --namespace），"
                    f"或为 {current} 指定独立的 --root；切勿带着错命名空间启动"
                ),
                configured=current,
                on_disk=sorted(prefixes),
            )
        else:
            check.add(OK, f"数据目录为空，将以新命名空间 {current} 首次启动")

    @staticmethod
    def _scan_namespace_prefixes(data_root: Path) -> dict[str, int]:
        prefixes: dict[str, int] = {}
        if not data_root.is_dir():
            return prefixes
        for path in data_root.rglob("*.json"):
            try:
                relative = path.relative_to(data_root)
            except ValueError:
                continue
            parts = relative.parts
            if len(parts) < 2:
                continue
            prefix = "/".join(parts[:2])
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        return prefixes

    # ------------------------------------------------------------- 5. 端口
    def _check_port(self) -> None:
        check = self._checks["port"]
        host, port = self.settings.host, self.settings.port
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            check.add(
                ERROR,
                f"监听地址无法解析：{host}（{exc.strerror or exc}）",
                fix="检查 serve --host 是否为本机网卡地址，常用 127.0.0.1 或 0.0.0.0",
                host=host,
                port=port,
            )
            return
        for family, socket_type, proto, _canonname, sockaddr in infos:
            probe = socket.socket(family, socket_type, proto)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(sockaddr)
            except OSError as exc:
                self._report_bind_failure(check, exc, host, port)
                return
            finally:
                probe.close()
        check.add(OK, f"{host}:{port} 可绑定，未被占用", host=host, port=port)

    def _report_bind_failure(self, check: CheckResult, exc: OSError, host: str, port: int) -> None:
        if exc.errno == errno.EADDRINUSE:
            holder = self._describe_port_holder(port)
            details: dict[str, Any] = {"host": host, "port": port}
            message = f"端口 {port} 已被占用，起在这里必然撞车"
            if holder:
                details["holder"] = holder
                label = holder.get("namespace") or holder.get("cmdline") or holder.get("pid")
                if holder.get("kind") == "flashsmelter":
                    message += f"：占用方是本平台实例（{label}）"
                else:
                    message += f"：占用进程 {label}"
            check.add(
                ERROR,
                message,
                fix=(
                    f"停掉占用进程，或用 serve --port 换一个端口；"
                    f"排查命令：ss -ltnp 'sport = :{port}' / lsof -iTCP:{port} -sTCP:LISTEN"
                ),
                **details,
            )
        elif exc.errno in (errno.EACCES, errno.EPERM):
            check.add(
                ERROR,
                f"无权在 {host}:{port} 上监听（{exc.strerror or exc}）",
                fix="1024 以下端口需要提权，建议改用 1024 以上端口",
                host=host,
                port=port,
            )
        elif exc.errno == errno.EADDRNOTAVAIL:
            check.add(
                ERROR,
                f"本机没有地址 {host}（{exc.strerror or exc}）",
                fix="用本机实际网卡地址，或 --host 127.0.0.1 / 0.0.0.0",
                host=host,
                port=port,
            )
        else:
            check.add(
                ERROR,
                f"端口绑定失败：{exc.strerror or exc}",
                fix="检查地址与端口配置后重试",
                host=host,
                port=port,
                errno=exc.errno,
            )

    def _describe_port_holder(self, port: int) -> dict[str, Any] | None:
        """尽力定位端口占用方：先读 /proc，再探一下是不是本平台控制台。"""

        holder = self._holder_from_proc(port)
        console = self._probe_console(port)
        if holder is None and console is None:
            return None
        merged: dict[str, Any] = dict(holder or {})
        if console:
            merged["kind"] = "flashsmelter"
            merged.update(console)
        elif "pid" in merged:
            merged.setdefault("kind", "process")
        return merged

    @staticmethod
    def _holder_from_proc(port: int) -> dict[str, Any] | None:
        if not sys.platform.startswith("linux"):
            return None
        target_hex = f"{port:04X}"
        inodes: set[str] = set()
        for table in ("tcp", "tcp6"):
            try:
                with open(f"/proc/net/{table}", encoding="utf-8") as handle:
                    lines = handle.readlines()[1:]
            except OSError:
                continue
            for line in lines:
                columns = line.split()
                if len(columns) < 10 or columns[3] != "0A":  # 0A = LISTEN
                    continue
                local = columns[1]
                if ":" not in local or local.rsplit(":", 1)[1] != target_hex:
                    continue
                inodes.add(columns[9])
        if not inodes:
            return None
        for pid in sorted(_iter_pids()):
            for inode in _socket_inodes_of_pid(pid):
                if inode in inodes:
                    return {"pid": pid, "cmdline": _read_cmdline(pid)}
        return {"inode": sorted(inodes)[0]}

    def _probe_console(self, port: int) -> dict[str, Any] | None:
        import http.client

        host = self.settings.host if self.settings.host not in ("0.0.0.0", "::") else "127.0.0.1"
        try:
            connection = http.client.HTTPConnection(host, port, timeout=0.8)
            try:
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                body = response.read(4096)
            finally:
                connection.close()
        except OSError:
            return None
        if response.status != 200:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or "furnace_state" not in payload:
            return None
        return {
            "console": True,
            "namespace": payload.get("namespace"),
            "generation": payload.get("generation"),
        }

    # ------------------------------------------------------------- 6. 单实例
    def _check_instance(self, namespace: Namespace | None) -> None:
        check = self._checks["instance"]
        if os.name != "posix":
            check.skipped_reason = "非 POSIX 平台无法加排他运行锁"
            check.add(
                WARN,
                "当前平台无法强制单实例，多开同根目录需靠运维流程避免",
                fix="在 Linux 上运行可获得自动的单实例保护",
            )
            return
        metadata = {
            "pid": os.getpid(),
            "version": __version__,
            "host": self.settings.host,
            "port": self.settings.port,
            "namespace": namespace.prefix if namespace else self.settings.namespace,
            "root": str(Path(self.settings.root)),
            "started_at": self.clock.timestamp_iso(),
        }
        try:
            self.lock.acquire(metadata)
        except OSError:
            holder = self.lock.read_holder()
            details = {"lock_file": str(self.lock.path)}
            if holder:
                details["holder"] = holder
                who = holder.get("namespace") or f"pid {holder.get('pid')}"
                message = f"状态根 {self.settings.root} 上已有实例在运行（{who}）"
                if holder.get("port"):
                    message += f"，监听 {holder.get('host')}:{holder.get('port')}"
            else:
                message = f"状态根 {self.settings.root} 已被另一个实例锁定"
            check.add(
                ERROR,
                message,
                fix="确认旧实例状态后停掉它，或为新实例指定独立 --root；不要双开同一状态根",
                **details,
            )
        else:
            check.add(OK, f"已取得 {self.lock.path} 排他运行锁")

    def release(self) -> None:
        self.lock.release()


def _iter_pids():
    proc = Path("/proc")
    if not proc.is_dir():
        return
    for entry in proc.iterdir():
        if entry.name.isdigit():
            yield int(entry.name)


def _socket_inodes_of_pid(pid: int) -> set[str]:
    inodes: set[str] = set()
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return inodes
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[len("socket:[") : -1])
    return inodes


def _read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


# ------------------------------------------------------------------ 文本报告
_STATUS_MARK = {OK: "[OK]", WARN: "[WARN]", ERROR: "[FAIL]", SKIPPED: "[SKIP]"}


def render_text(report: PreflightReport) -> str:
    """渲染成值班人员可读的自检报告：每项结论 + 问题在哪 + 怎么改。"""

    lines = ["FlashSmelter 启动前自检" if report.ok else "FlashSmelter 启动前自检：存在未通过项，禁止启动"]
    for check in report.checks:
        lines.append(f"{_STATUS_MARK[check.status]} {check.title} —— {check.summary}")
        if check.status == SKIPPED and check.skipped_reason:
            lines.append(f"    - {check.skipped_reason}")
        for finding in check.findings:
            if finding.level == OK:
                continue
            lines.append(f"    - {finding.message}")
            if finding.details:
                lines.append(f"      定位：{json.dumps(finding.details, ensure_ascii=False, sort_keys=True)}")
            if finding.fix:
                lines.append(f"      修改：{finding.fix}")
    if report.ok:
        tail = "全部检查通过，可以启动"
        if report.warnings:
            tail += f"（{report.warnings} 个告警不阻塞启动）"
        lines.append(tail)
    else:
        lines.append(
            f"共 {report.problems} 个问题未通过，自检拒绝放行；"
            "按上面的「修改」建议处理后重跑 doctor 或 serve"
        )
    return "\n".join(lines)


__all__ = [
    "Preflight",
    "PreflightReport",
    "CheckResult",
    "Finding",
    "InstanceLock",
    "render_text",
    "CHECK_ORDER",
]
