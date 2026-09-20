"""启动前自检：配置、数据目录、端口、依赖四组检查与 serve 启动门控。"""

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
from flashsmelter.preflight import run_preflight

from .helpers import make_root


def _report(**overrides):
    listen = overrides.pop("listen", {})
    environ = overrides.pop("environ", {})
    settings = Settings(root=overrides.pop("root", make_root()), **overrides)
    return run_preflight(settings, environ=environ, **listen)


def _named(results, name):
    return next(item for item in results if item.name == name)


class PreflightConfigTest(unittest.TestCase):
    def test_clean_settings_pass(self) -> None:
        report = _report()
        self.assertTrue(report.ok)
        ranges = _named(report.results, "config.ranges")
        self.assertEqual("ok", ranges.severity)
        ns = _named(report.results, "config.namespace")
        self.assertEqual("smelter/line1", ns.details["namespace"]["prefix"])

    def test_bad_range_is_error_with_fix(self) -> None:
        report = _report(oxygen_enrichment_min=0.9)
        self.assertFalse(report.ok)
        item = _named(report.results, "config.ranges")
        self.assertEqual("error", item.severity)
        self.assertIn("富氧", item.summary)
        self.assertIsNotNone(item.fix)
        self.assertEqual(0.9, item.details["details"]["min"])

    def test_bad_namespace_is_error_with_fix(self) -> None:
        report = _report(namespace="line1")
        item = _named(report.results, "config.namespace")
        self.assertEqual("error", item.severity)
        self.assertIn("site/unit", item.fix)

    def test_unknown_env_var_is_warning_not_error(self) -> None:
        report = _report(environ={"FLASHSMELTER_PRT": "9000", "OTHER": "x"})
        self.assertTrue(report.ok, "拼错的环境变量只警告，不阻断启动")
        item = _named(report.results, "config.env_typos")
        self.assertEqual("warning", item.severity)
        self.assertEqual(["FLASHSMELTER_PRT"], item.details["unknown"])

    def test_known_env_vars_not_flagged(self) -> None:
        report = _report(environ={"FLASHSMELTER_PORT": "9000", "FLASHSMELTER_ROOT": "/tmp/x"})
        item = _named(report.results, "config.env_typos")
        self.assertEqual("ok", item.severity)


class PreflightStorageTest(unittest.TestCase):
    def test_directories_created_and_probed(self) -> None:
        root = make_root() / "nested" / "state"
        report = _report(root=root)
        self.assertTrue((root / "data").is_dir())
        self.assertTrue((root / "journal").is_dir())
        self.assertEqual("ok", _named(report.results, "storage.directories").severity)
        self.assertEqual("ok", _named(report.results, "storage.write_probe").severity)
        self.assertFalse((root / ".preflight-probe").exists(), "探针文件用完即删")

    def test_unwritable_root_fails(self) -> None:
        if os.geteuid() == 0:  # root 对只读位免疫，容器里跑会误判
            self.skipTest("root 用户绕过文件权限位")
        parent = make_root()
        os.chmod(parent, 0o500)
        try:
            report = _report(root=parent / "state")
        finally:
            os.chmod(parent, 0o700)
        self.assertFalse(report.ok)
        self.assertEqual("error", _named(report.results, "storage.directories").severity)

    def test_corrupted_record_fails_integrity(self) -> None:
        root = make_root()
        (root / "data" / "smelter" / "line1" / "x").mkdir(parents=True)
        (root / "data" / "smelter" / "line1" / "x" / "state.json").write_text(
            json.dumps(
                {
                    "key": "smelter/line1/x/state",
                    "version": 1,
                    "written_at": "2026-01-01T00:00:00.000000+0000",
                    "checksum": "deadbeef",
                    "payload": {},
                }
            ),
            encoding="utf-8",
        )
        report = _report(root=root)
        item = _named(report.results, "storage.integrity")
        self.assertEqual("error", item.severity)
        self.assertTrue(any("smelter/line1/x/state" in problem for problem in item.details["problems"]))
        self.assertIn("verify", item.fix)

    def test_foreign_namespace_records_warn(self) -> None:
        root = make_root()
        (root / "data" / "smelter" / "line2" / "x").mkdir(parents=True)
        # 借用真实 store 写一条合法的「别人家」记录。
        from flashsmelter.runtime import ManualClock
        from flashsmelter.store import DurableStore

        foreign = DurableStore(root, clock=ManualClock())
        foreign.put("smelter/line2/x/state", {"a": 1})
        report = _report(root=root, namespace="smelter/line1")
        item = _named(report.results, "storage.namespace_isolation")
        self.assertEqual("warning", item.severity)
        self.assertEqual(1, item.details["foreign_records"]["smelter/line2"])
        self.assertTrue(report.ok, "串扰是警告，不阻断；硬错误由占用方自检负责")

    def test_foreign_audit_events_warn(self) -> None:
        root = make_root()
        journal = root / "journal" / "audit"
        journal.mkdir(parents=True)
        from flashsmelter.runtime import ManualClock
        from flashsmelter.store import DurableStore
        from flashsmelter.ns import Namespace
        from flashsmelter.audit import AuditLog

        store = DurableStore(root, clock=ManualClock())
        audit = AuditLog(store, Namespace.parse("smelter/line9"), ManualClock())
        audit.record(actor="a", action="start", target="t", outcome="ok", correlation_id="c")
        report = _report(root=root, namespace="smelter/line1")
        item = _named(report.results, "storage.namespace_isolation")
        self.assertEqual("warning", item.severity)
        self.assertEqual(1, item.details["foreign_audit_events"]["smelter/line9"])


class PreflightPortTest(unittest.TestCase):
    def test_free_port_ok(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        report = _report(listen={"host": "127.0.0.1", "port": free_port})
        item = _named(report.results, "port.bind")
        self.assertEqual("ok", item.severity)

    def test_occupied_port_fails(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            report = _report(listen={"host": "127.0.0.1", "port": port})
        finally:
            holder.close()
        self.assertFalse(report.ok)
        item = _named(report.results, "port.bind")
        self.assertEqual("error", item.severity)
        self.assertIn("占用", item.summary)
        self.assertIn("--port", item.fix)

    def test_unavailable_host_fails(self) -> None:
        report = _report(listen={"host": "192.0.2.10", "port": 8091})
        item = _named(report.results, "port.bind")
        self.assertEqual("error", item.severity)
        self.assertIn("192.0.2.10", item.summary)

    def test_out_of_range_port_fails(self) -> None:
        report = _report(listen={"host": "127.0.0.1", "port": 70000})
        item = _named(report.results, "port.bind")
        self.assertEqual("error", item.severity)


class PreflightDependencyTest(unittest.TestCase):
    def test_python_and_modules_ok(self) -> None:
        report = _report()
        self.assertEqual("ok", _named(report.results, "dependency.python").severity)
        self.assertEqual("ok", _named(report.results, "dependency.modules").severity)


class PreflightReportShapeTest(unittest.TestCase):
    def test_dict_and_text_shapes(self) -> None:
        report = _report()
        payload = report.to_dict()
        self.assertTrue(payload["ok"])
        groups = {item["group"] for item in payload["results"]}
        self.assertEqual({"config", "storage", "port", "dependency"}, groups)
        text = report.render_text()
        self.assertIn("[配置]", text)
        self.assertIn("[数据目录]", text)
        self.assertIn("[端口]", text)
        self.assertIn("[依赖]", text)
        self.assertIn("可以启动", text)

    def test_every_error_carries_fix(self) -> None:
        report = _report(namespace="nope", listen={"host": "127.0.0.1", "port": 70000})
        self.assertFalse(report.ok)
        for item in report.errors:
            self.assertTrue(item.fix, msg=item.name)
        self.assertIn("阻止启动", report.render_text())


def _run(*args: str, root: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    # 子进程不继承测试里可能存在的 FLASHSMELTER_* 干扰项
    merged = {k: v for k, v in merged.items() if not k.startswith("FLASHSMELTER_")}
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "flashsmelter", "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=merged,
    )


class CliDoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root()

    def test_doctor_ok_exit_zero(self) -> None:
        completed = _run("doctor", root=self.root)
        self.assertEqual(0, completed.returncode, msg=completed.stdout)
        self.assertIn("自检通过", completed.stdout)

    def test_doctor_json(self) -> None:
        completed = _run("doctor", "--json", root=self.root)
        self.assertEqual(0, completed.returncode, msg=completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(4, len({item["group"] for item in payload["results"]}))

    def test_doctor_port_collision_exit_3(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            completed = _run("doctor", "--port", str(port), root=self.root)
        finally:
            holder.close()
        self.assertEqual(3, completed.returncode)
        self.assertIn("已被占用", completed.stdout)
        self.assertIn("改法", completed.stdout)

    def test_doctor_bad_env_reported_uniformly(self) -> None:
        completed = _run(
            "doctor",
            root=self.root,
            env={"FLASHSMELTER_OXYGEN_ENRICHMENT_MIN": "0.9"},
        )
        self.assertEqual(3, completed.returncode)
        self.assertIn("config.boot", completed.stdout)
        self.assertIn("阻止启动", completed.stdout)


class ServeGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root()

    def _free_port(self) -> int:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_serve_blocked_on_port_collision(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            completed = _run("serve", "--port", str(port), root=self.root)
        finally:
            holder.close()
        self.assertEqual(3, completed.returncode)
        self.assertIn("自检未通过", completed.stdout)
        self.assertIn("已被占用", completed.stdout)

    def test_serve_starts_when_preflight_passes(self) -> None:
        port = self._free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "flashsmelter", "--root", str(self.root),
             "serve", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("FLASHSMELTER_")},
        )
        try:
            deadline = time.time() + 15
            started = False
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        started = True
                        break
                except OSError:
                    time.sleep(0.2)
            self.assertTrue(started, msg="自检通过后控制台应正常监听")
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_serve_skip_preflight_on_collision_falls_back_to_bind_error(self) -> None:
        # 跳过自检时仍由实际绑定兜底，不会「带病起来」。
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        try:
            completed = _run("serve", "--port", str(busy), "--skip-preflight", root=self.root)
        finally:
            holder.close()
        self.assertEqual(3, completed.returncode)
        self.assertIn("跳过启动自检", completed.stdout)
        self.assertIn("绑定", completed.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
