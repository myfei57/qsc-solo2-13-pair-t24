"""启动前自检（preflight）：配置、命名空间、数据目录、端口、依赖、单实例。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

from flashsmelter.config import Settings
from flashsmelter.preflight import (
    ERROR,
    OK,
    SKIPPED,
    WARN,
    InstanceLock,
    Preflight,
    render_text,
)
from flashsmelter.runtime import ManualClock
from flashsmelter.store import DurableStore

from .helpers import make_root


def write_record(root: Path, namespace: str, component: str = "oxygen") -> None:
    """在给定根目录下写出一份带合法校验和的命名空间落盘记录。"""

    store = DurableStore(root, clock=ManualClock())
    store.put(f"{namespace}/{component}/state", {"state": "cold"})


def run(settings: Settings, *, config_issues=()):
    preflight = Preflight(settings, config_issues=list(config_issues))
    report = preflight.run()
    preflight.release()
    return report


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class ConfigCheckTest(unittest.TestCase):
    def test_defaults_pass(self) -> None:
        settings, issues = Settings.load({}, root=make_root(), port=free_port())
        report = run(settings, config_issues=issues)
        self.assertTrue(report.ok, msg=render_text(report))
        config = report.get("config")
        self.assertEqual(OK, config.status)

    def test_all_config_violations_reported_together(self) -> None:
        env = {
            "FLASHSMELTER_PORT": "nope",
            "FLASHSMELTER_OXYGEN_ENRICHMENT_MIN": "0.9",
            "FLASHSMELTER_REQUEST_TIMEOUT_SECONDS": "-1",
        }
        settings, issues = Settings.load(env, root=make_root(), port=0)
        report = run(settings, config_issues=issues)
        config = report.get("config")
        self.assertEqual(ERROR, config.status)
        messages = [f.message for f in config.findings if f.level == ERROR]
        self.assertTrue(any("PORT" in m for m in messages))
        self.assertTrue(any("富氧浓度量程" in m for m in messages))
        self.assertTrue(any("端口超出范围" in m for m in messages))
        self.assertTrue(any("请求超时" in m for m in messages))
        # 端口越界时端口探测应跳过，而不是带着 1 去绑定。
        self.assertEqual(SKIPPED, report.get("port").status)
        self.assertFalse(report.ok)

    def test_port_out_of_range_is_skipped_not_bound(self) -> None:
        settings, _issues = Settings.load({}, root=make_root(), port=0)
        report = run(settings, config_issues=_issues)
        self.assertEqual(SKIPPED, report.get("port").status)
        self.assertFalse(report.ok)

    def test_bad_namespace_blocked(self) -> None:
        settings = Settings(root=make_root(), port=free_port(), namespace="no-slash")
        report = run(settings)
        self.assertEqual(ERROR, report.get("config").status)
        self.assertEqual(SKIPPED, report.get("namespace").status)

    def test_every_problem_carries_fix_advice(self) -> None:
        settings, issues = Settings.load({"FLASHSMELTER_PORT": "x"}, root=make_root())
        report = run(settings, config_issues=issues)
        for check in report.checks:
            for finding in check.findings:
                if finding.level == ERROR:
                    self.assertTrue(finding.fix, msg=f"问题缺少修改建议：{finding.message}")


class NamespaceCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root()
        write_record(self.root, "smelter/line1")

    def test_matching_namespace_passes(self) -> None:
        settings = Settings(root=self.root, port=free_port(), namespace="smelter/line1")
        report = run(settings)
        self.assertEqual(OK, report.get("namespace").status)

    def test_mismatched_namespace_fails_with_pairing_detail(self) -> None:
        settings = Settings(root=self.root, port=free_port(), namespace="smelter/line2")
        report = run(settings)
        check = report.get("namespace")
        self.assertEqual(ERROR, check.status)
        finding = next(f for f in check.findings if f.level == ERROR)
        self.assertIn("不配对", finding.message)
        self.assertEqual("smelter/line2", finding.details["configured"])
        self.assertEqual(["smelter/line1"], finding.details["on_disk"])

    def test_foreign_namespace_in_shared_root_warns(self) -> None:
        write_record(self.root, "smelter/line9", component="furnace")
        settings = Settings(root=self.root, port=free_port(), namespace="smelter/line1")
        report = run(settings)
        check = report.get("namespace")
        self.assertEqual(WARN, check.status)
        self.assertTrue(any("line9" in f.message for f in check.findings if f.level == WARN))


class DataCheckTest(unittest.TestCase):
    def test_root_occupied_by_file_fails(self) -> None:
        root = make_root() / "blocker"
        root.write_text("x", encoding="utf-8")
        settings = Settings(root=root, port=free_port())
        report = run(settings)
        data = report.get("data")
        self.assertEqual(ERROR, data.status)
        self.assertTrue(any("不是目录" in f.message for f in data.findings))
        self.assertEqual(SKIPPED, report.get("namespace").status)

    def test_readonly_root_fails(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root 用户绕过权限位")
        root = make_root()
        os.chmod(root, 0o500)
        try:
            settings = Settings(root=root / "nested", port=free_port())
            report = run(settings)
            self.assertEqual(ERROR, report.get("data").status)
        finally:
            os.chmod(root, 0o700)

    def test_corrupt_record_fails(self) -> None:
        root = make_root()
        target = root / "data" / "smelter" / "line1" / "furnace" / "state.json"
        target.parent.mkdir(parents=True)
        target.write_text("{not json", encoding="utf-8")
        settings = Settings(root=root, port=free_port(), namespace="smelter/line1")
        report = run(settings)
        data = report.get("data")
        self.assertEqual(ERROR, data.status)
        self.assertTrue(any("落盘数据损坏" in f.message for f in data.findings))


class PortCheckTest(unittest.TestCase):
    def test_free_port_passes(self) -> None:
        settings = Settings(root=make_root(), port=free_port())
        report = run(settings)
        self.assertEqual(OK, report.get("port").status)

    def test_occupied_port_fails_and_identifies_holder(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            settings = Settings(root=make_root(), port=port)
            report = run(settings)
            check = report.get("port")
            self.assertEqual(ERROR, check.status)
            finding = next(f for f in check.findings if f.level == ERROR)
            self.assertIn("已被占用", finding.message)
            self.assertEqual(port, finding.details["port"])
            self.assertIn("ss -ltnp", finding.fix)
        finally:
            listener.close()

    def test_unresolvable_host_fails(self) -> None:
        settings = Settings(root=make_root(), port=free_port(), host="no-such-host.invalid")
        report = run(settings)
        self.assertEqual(ERROR, report.get("port").status)

    def test_privileged_port_fails_without_capability(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 80))
        except OSError:
            pass  # 确实无特权端口能力，自检应当报同样的问题
        else:
            self.skipTest("当前环境允许绑定特权端口")
        finally:
            probe.close()
        settings = Settings(root=make_root(), port=80)
        report = run(settings)
        self.assertEqual(ERROR, report.get("port").status)


class DependencyCheckTest(unittest.TestCase):
    def test_runtime_dependencies_ok(self) -> None:
        settings = Settings(root=make_root(), port=free_port())
        report = run(settings)
        self.assertEqual(OK, report.get("dependencies").status)


class InstanceLockTest(unittest.TestCase):
    def test_second_holder_blocked_and_reports_owner(self) -> None:
        root = make_root()
        first = InstanceLock(root)
        first.acquire({"pid": 11111, "namespace": "smelter/line1", "port": 9000})
        try:
            settings = Settings(root=root, port=free_port())
            report = run(settings)
            check = report.get("instance")
            self.assertEqual(ERROR, check.status)
            finding = next(f for f in check.findings if f.level == ERROR)
            self.assertEqual(11111, finding.details["holder"]["pid"])
            self.assertIn("已有实例", finding.message)
        finally:
            first.release()

    def test_lock_released_after_holder_exit(self) -> None:
        root = make_root()
        first = InstanceLock(root)
        first.acquire({"pid": 11111})
        first.release()
        second = InstanceLock(root)
        second.acquire({"pid": 22222})
        second.release()


class ReportRenderTest(unittest.TestCase):
    def test_text_report_lists_problem_fix_and_blocking_line(self) -> None:
        settings, issues = Settings.load({"FLASHSMELTER_PORT": "bad"}, root=make_root())
        report = run(settings, config_issues=issues)
        text = render_text(report)
        self.assertIn("禁止启动", text)
        self.assertIn("修改：", text)
        payload = report.to_dict()
        self.assertFalse(payload["ok"])
        self.assertGreaterEqual(payload["problems"], 1)
        ids = [check["id"] for check in payload["checks"]]
        self.assertEqual(
            ["config", "namespace", "data", "port", "dependencies", "instance"], ids
        )


def _run_cli(*args: str, root: Path, env: dict[str, str] | None = None, timeout: float = 20.0):
    merged = dict(os.environ)
    merged["PYTHONPATH"] = "/workspace"
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "flashsmelter", "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=merged,
        timeout=timeout,
    )


class DoctorCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("flashsmelter-doctor-")

    def test_doctor_exit_codes_and_json(self) -> None:
        ok = _run_cli("doctor", root=self.root)
        self.assertEqual(0, ok.returncode, msg=ok.stdout + ok.stderr)
        self.assertIn("全部检查通过", ok.stdout)
        payload = json.loads(_run_cli("doctor", "--json", root=self.root).stdout)
        self.assertTrue(payload["ok"])
        bad = _run_cli(
            "doctor",
            root=self.root,
            env={"FLASHSMELTER_OXYGEN_ENRICHMENT_MIN": "0.95"},
        )
        self.assertEqual(3, bad.returncode)
        self.assertIn("富氧浓度量程", bad.stdout)


class ServePreflightCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("flashsmelter-serve-")

    def test_serve_refuses_on_occupied_port(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            completed = _run_cli("serve", "--port", str(port), root=self.root, timeout=20.0)
        finally:
            listener.close()
        self.assertEqual(3, completed.returncode)
        self.assertIn("已被占用", completed.stdout)

    def test_serve_starts_after_preflight_and_serves_health(self) -> None:
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "flashsmelter", "--root", str(self.root),
             "serve", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONPATH": "/workspace"},
        )
        try:
            self._wait_for_health(port)
            # 同根目录再起一个：换端口也必须被单实例检查挡住。
            blocked = _run_cli("serve", "--port", str(free_port()), root=self.root, timeout=20.0)
            self.assertEqual(3, blocked.returncode)
            self.assertIn("已有实例", blocked.stdout)
        finally:
            proc.send_signal(subprocess.signal.SIGINT)
            try:
                output, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
        self.assertIn("控制台已启动", output)

    @staticmethod
    def _wait_for_health(port: int) -> None:
        import http.client

        deadline = time.monotonic() + 10
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
                conn.request("GET", "/api/health")
                response = conn.getresponse()
                body = response.read()
                conn.close()
                if response.status == 200:
                    payload = json.loads(body)
                    if payload.get("status") == "ok":
                        return
            except OSError as exc:
                last_error = exc
            time.sleep(0.2)
        raise AssertionError(f"控制台未在时限内就绪：{last_error}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
