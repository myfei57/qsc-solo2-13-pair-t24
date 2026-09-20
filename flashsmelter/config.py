"""运行期配置。

配置全部来自显式参数或 ``FLASHSMELTER_*`` 环境变量，带工艺量程校验；量程本身
就是门控的一部分（例如富氧浓度下限、余热锅炉汽包水位下限），因此校验失败要
在启动阶段直接拒绝，而不是等到运行时。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple, Mapping

from .errors import ConfigurationError, ValidationError

ENV_PREFIX = "FLASHSMELTER_"


class ConfigIssue(NamedTuple):
    """单条配置问题：问题描述 + 定位细节 + 修改建议。"""

    message: str
    details: dict[str, Any]
    fix: str

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "details": dict(self.details), "fix": self.fix}


def _read_env(environ: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, caster in _ENV_FIELDS.items():
        raw = environ.get(ENV_PREFIX + name.upper())
        if raw is None or raw == "":
            continue
        try:
            values[name] = caster(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"环境变量 {ENV_PREFIX}{name.upper()} 取值无法解析",
                details={"value": raw, "expected": caster.__name__},
            ) from exc
    return values


def _read_env_lenient(environ: Mapping[str, str]) -> tuple[dict[str, Any], list[ConfigIssue]]:
    """与 :func:`_read_env` 相同的解析，但不抛异常：解析失败的变量逐条收集。

    供启动自检使用——自检要把所有配置问题一次报全，而不是撞上第一个坏变量就停。
    """

    values: dict[str, Any] = {}
    issues: list[ConfigIssue] = []
    for name, caster in _ENV_FIELDS.items():
        env_name = ENV_PREFIX + name.upper()
        raw = environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            values[name] = caster(raw)
        except (TypeError, ValueError):
            issues.append(
                ConfigIssue(
                    message=f"环境变量 {env_name} 取值无法解析",
                    details={"value": raw, "expected": caster.__name__},
                    fix=f"把 {env_name} 改成 {caster.__name__} 能解析的值，或取消该变量后重试",
                )
            )
    return values, issues


_ENV_FIELDS: dict[str, Any] = {
    "host": str,
    "port": int,
    "namespace": str,
    "request_timeout_seconds": float,
    "max_body_bytes": int,
    "audit_page_limit": int,
    "burner_record_max_age_seconds": float,
    "burner_min_stable_hold_seconds": float,
    "burner_fuel_pressure_min_kpa": float,
    "burner_fuel_pressure_max_kpa": float,
    "burner_air_flow_min_nm3h": float,
    "oxygen_enrichment_min": float,
    "oxygen_enrichment_max": float,
    "oxygen_baseline_window_seconds": float,
    "oxygen_ramp_step": float,
    "oxygen_flow_min_nm3h": float,
    "oxygen_analyzer_tolerance": float,
    "feed_rate_max_tph": float,
    "heat_feed_budget_tons": float,
    "settler_min_bath_level_m": float,
    "settler_layering_dwell_seconds": float,
    "slag_min_thickness_m": float,
    "slag_tap_rate_tph": float,
    "matte_min_level_m": float,
    "matte_tap_rate_tph": float,
    "converter_batch_max_tons": float,
    "waste_drum_level_min": float,
    "waste_exhaust_temp_max_c": float,
    "waste_latch_min_hold_seconds": float,
    "furnace_purge_seconds": float,
    "furnace_min_smelt_dwell_seconds": float,
    "furnace_transition_timeout_seconds": float,
}


@dataclass(frozen=True, slots=True)
class Settings:
    """平台配置。字段默认值即一条正常运行的产线。"""

    root: Path = field(default_factory=lambda: Path("var"))
    host: str = "127.0.0.1"
    port: int = 8080
    namespace: str = "smelter/line1"
    request_timeout_seconds: float = 5.0
    max_body_bytes: int = 65536
    audit_page_limit: int = 200

    # 燃烧器状态必须已在窗口内落盘，精矿喷吹才允许 armed。
    burner_record_max_age_seconds: float = 120.0
    burner_min_stable_hold_seconds: float = 20.0
    burner_fuel_pressure_min_kpa: float = 120.0
    burner_fuel_pressure_max_kpa: float = 320.0
    burner_air_flow_min_nm3h: float = 3500.0

    # 富氧系统量程与分析仪基线窗口。
    oxygen_enrichment_min: float = 0.45
    oxygen_enrichment_max: float = 0.85
    oxygen_baseline_window_seconds: float = 300.0
    oxygen_ramp_step: float = 0.02
    oxygen_flow_min_nm3h: float = 8000.0
    oxygen_analyzer_tolerance: float = 0.01

    # 精矿喷吹与热料预算。
    feed_rate_max_tph: float = 180.0
    heat_feed_budget_tons: float = 1200.0

    # 沉淀池分层与放渣放铜阈值。
    settler_min_bath_level_m: float = 0.35
    settler_layering_dwell_seconds: float = 60.0
    slag_min_thickness_m: float = 0.08
    slag_tap_rate_tph: float = 40.0
    matte_min_level_m: float = 0.20
    matte_tap_rate_tph: float = 55.0

    # 转炉单批上限。
    converter_batch_max_tons: float = 90.0

    # 余热锅炉联锁。
    waste_drum_level_min: float = 0.40
    waste_exhaust_temp_max_c: float = 380.0
    waste_latch_min_hold_seconds: float = 30.0

    # 闪速炉编排。
    furnace_purge_seconds: float = 15.0
    furnace_min_smelt_dwell_seconds: float = 45.0
    furnace_transition_timeout_seconds: float = 600.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> "Settings":
        env = os.environ if environ is None else environ
        values = _read_env(env)
        root = env.get(ENV_PREFIX + "ROOT")
        if root:
            values["root"] = Path(root)
        values.update({key: value for key, value in overrides.items() if value is not None})
        settings = cls(**values)
        settings.validate()
        return settings

    @classmethod
    def load(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        root: Path | str | None = None,
        namespace: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> tuple["Settings", list[ConfigIssue]]:
        """宽容装载：坏变量、超量程、坏命名空间都不抛异常。

        返回「能装出来的 Settings（可能仍不自洽）」与逐条问题清单，专供启动
        自检使用；自检可以据此把所有问题一次报全。
        """

        env = os.environ if environ is None else environ
        values, issues = _read_env_lenient(env)
        env_root = env.get(ENV_PREFIX + "ROOT")
        if env_root:
            values["root"] = Path(env_root)
        if root is not None:
            values["root"] = Path(root)
        if namespace is not None:
            values["namespace"] = namespace
        if host is not None:
            values["host"] = host
        if port is not None:
            values["port"] = port
        # 未知字段通常来自拼写错误的环境变量；dataclass 直接构造会抛 TypeError，
        # 自检阶段同样要兜住。
        known = set(cls.__slots__)
        unknown = sorted(set(values) - known)
        if unknown:
            for name in unknown:
                issues.append(
                    ConfigIssue(
                        message=f"未知配置项 {name}",
                        details={"field": name},
                        fix="检查 FLASHSMELTER_* 环境变量拼写，未知项不会生效",
                    )
                )
            values = {name: value for name, value in values.items() if name in known}
        settings = cls(**values)
        issues.extend(settings.iter_violations(env=env))
        return settings, issues

    def iter_violations(self, *, env: Mapping[str, str] | None = None) -> list[ConfigIssue]:
        """量程与互斥关系逐条检查，返回全部违例而不是遇到第一条就抛。"""

        def fix(env_name: str) -> str:
            if env is not None and env_name in env:
                return f"修正环境变量 {env_name} 后重试"
            return f"修正 {env_name[len(ENV_PREFIX):].lower()} 配置后重试"

        issues: list[ConfigIssue] = []

        def record(message: str, details: dict[str, Any], fix: str) -> None:
            issues.append(ConfigIssue(message=message, details=details, fix=fix))

        if not 1 <= self.port <= 65535:
            record(
                "监听端口超出范围",
                {"port": self.port},
                fix(ENV_PREFIX + "PORT"),
            )
        if self.request_timeout_seconds <= 0:
            record(
                "请求超时必须为正数",
                {"request_timeout_seconds": self.request_timeout_seconds},
                fix(ENV_PREFIX + "REQUEST_TIMEOUT_SECONDS"),
            )
        if self.max_body_bytes < 1024:
            record(
                "请求体上限过小",
                {"max_body_bytes": self.max_body_bytes},
                fix(ENV_PREFIX + "MAX_BODY_BYTES"),
            )
        if self.audit_page_limit < 1:
            record(
                "审计分页上限必须为正",
                {"audit_page_limit": self.audit_page_limit},
                fix(ENV_PREFIX + "AUDIT_PAGE_LIMIT"),
            )
        if not 0 < self.oxygen_enrichment_min < self.oxygen_enrichment_max < 1:
            record(
                "富氧浓度量程不合法",
                {"min": self.oxygen_enrichment_min, "max": self.oxygen_enrichment_max},
                f"要求 0 < min < max < 1：检查 {ENV_PREFIX}OXYGEN_ENRICHMENT_MIN / {ENV_PREFIX}OXYGEN_ENRICHMENT_MAX",
            )
        if self.oxygen_ramp_step <= 0:
            record(
                "富氧爬坡步长必须为正",
                {"step": self.oxygen_ramp_step},
                fix(ENV_PREFIX + "OXYGEN_RAMP_STEP"),
            )
        if self.oxygen_baseline_window_seconds <= 0:
            record(
                "分析仪基线窗口必须为正",
                {"window": self.oxygen_baseline_window_seconds},
                fix(ENV_PREFIX + "OXYGEN_BASELINE_WINDOW_SECONDS"),
            )
        if self.burner_record_max_age_seconds <= 0:
            record(
                "燃烧器落盘时效窗口必须为正",
                {"window": self.burner_record_max_age_seconds},
                fix(ENV_PREFIX + "BURNER_RECORD_MAX_AGE_SECONDS"),
            )
        if self.burner_min_stable_hold_seconds < 0:
            record(
                "燃烧器稳定保持时长不能为负",
                {"hold": self.burner_min_stable_hold_seconds},
                fix(ENV_PREFIX + "BURNER_MIN_STABLE_HOLD_SECONDS"),
            )
        if not 0 < self.burner_fuel_pressure_min_kpa < self.burner_fuel_pressure_max_kpa:
            record(
                "燃烧器燃料压力量程不合法",
                {
                    "min": self.burner_fuel_pressure_min_kpa,
                    "max": self.burner_fuel_pressure_max_kpa,
                },
                f"要求 0 < min < max：检查 {ENV_PREFIX}BURNER_FUEL_PRESSURE_MIN_KPA / {ENV_PREFIX}BURNER_FUEL_PRESSURE_MAX_KPA",
            )
        if self.burner_air_flow_min_nm3h <= 0:
            record(
                "燃烧器助燃风下限必须为正",
                {"min": self.burner_air_flow_min_nm3h},
                fix(ENV_PREFIX + "BURNER_AIR_FLOW_MIN_NM3H"),
            )
        if self.oxygen_flow_min_nm3h <= 0:
            record(
                "富氧流量下限必须为正",
                {"min": self.oxygen_flow_min_nm3h},
                fix(ENV_PREFIX + "OXYGEN_FLOW_MIN_NM3H"),
            )
        if self.oxygen_analyzer_tolerance <= 0:
            record(
                "分析仪偏差容限必须为正",
                {"tolerance": self.oxygen_analyzer_tolerance},
                fix(ENV_PREFIX + "OXYGEN_ANALYZER_TOLERANCE"),
            )
        if self.feed_rate_max_tph <= 0:
            record(
                "喷吹上限必须为正",
                {"rate": self.feed_rate_max_tph},
                fix(ENV_PREFIX + "FEED_RATE_MAX_TPH"),
            )
        if self.heat_feed_budget_tons < self.feed_rate_max_tph:
            record(
                "单炉热料预算小于单次喷吹上限",
                {
                    "budget": self.heat_feed_budget_tons,
                    "rate": self.feed_rate_max_tph,
                },
                f"热料预算必须 ≥ 单次喷吹上限：检查 {ENV_PREFIX}HEAT_FEED_BUDGET_TONS / {ENV_PREFIX}FEED_RATE_MAX_TPH",
            )
        if self.settler_layering_dwell_seconds <= 0:
            record(
                "分层静置时长必须为正",
                {"dwell": self.settler_layering_dwell_seconds},
                fix(ENV_PREFIX + "SETTLER_LAYERING_DWELL_SECONDS"),
            )
        if not 0 < self.slag_min_thickness_m < self.settler_min_bath_level_m:
            record(
                "渣层下限必须小于沉淀池最低液位",
                {
                    "slag": self.slag_min_thickness_m,
                    "bath": self.settler_min_bath_level_m,
                },
                f"要求 0 < 渣层下限 < 沉淀池最低液位：检查 {ENV_PREFIX}SLAG_MIN_THICKNESS_M / {ENV_PREFIX}SETTLER_MIN_BATH_LEVEL_M",
            )
        if self.matte_min_level_m >= self.settler_min_bath_level_m:
            record(
                "冰铜液位下限必须小于沉淀池最低液位",
                {"matte": self.matte_min_level_m, "bath": self.settler_min_bath_level_m},
                f"检查 {ENV_PREFIX}MATTE_MIN_LEVEL_M / {ENV_PREFIX}SETTLER_MIN_BATH_LEVEL_M",
            )
        if self.converter_batch_max_tons <= 0:
            record(
                "转炉单批上限必须为正",
                {"max": self.converter_batch_max_tons},
                fix(ENV_PREFIX + "CONVERTER_BATCH_MAX_TONS"),
            )
        if not 0 < self.waste_drum_level_min < 1:
            record(
                "汽包水位下限必须是 (0,1) 区间比例",
                {"min": self.waste_drum_level_min},
                fix(ENV_PREFIX + "WASTE_DRUM_LEVEL_MIN"),
            )
        if self.waste_exhaust_temp_max_c <= 0:
            record(
                "烟气温度上限必须为正",
                {"max": self.waste_exhaust_temp_max_c},
                fix(ENV_PREFIX + "WASTE_EXHAUST_TEMP_MAX_C"),
            )
        if self.waste_latch_min_hold_seconds <= 0:
            record(
                "闩锁最短保持时长必须为正",
                {"hold": self.waste_latch_min_hold_seconds},
                fix(ENV_PREFIX + "WASTE_LATCH_MIN_HOLD_SECONDS"),
            )
        if self.furnace_purge_seconds <= 0:
            record(
                "吹扫时长必须为正",
                {"purge": self.furnace_purge_seconds},
                fix(ENV_PREFIX + "FURNACE_PURGE_SECONDS"),
            )
        if self.furnace_min_smelt_dwell_seconds < 0:
            record(
                "熔炼静置时长不能为负",
                {"dwell": self.furnace_min_smelt_dwell_seconds},
                fix(ENV_PREFIX + "FURNACE_MIN_SMELT_DWELL_SECONDS"),
            )
        if self.furnace_transition_timeout_seconds <= self.furnace_purge_seconds:
            record(
                "编排超时不得小于吹扫时长",
                {
                    "timeout": self.furnace_transition_timeout_seconds,
                    "purge": self.furnace_purge_seconds,
                },
                f"检查 {ENV_PREFIX}FURNACE_TRANSITION_TIMEOUT_SECONDS / {ENV_PREFIX}FURNACE_PURGE_SECONDS",
            )
        return issues

    def validate(self) -> None:
        violations = self.iter_violations()
        if violations:
            first = violations[0]
            raise ValidationError(first.message, details=first.details)

    def with_root(self, root: Path | str) -> "Settings":
        updated = replace(self, root=Path(root))
        updated.validate()
        return updated

    def ensure_directories(self) -> Path:
        root = Path(self.root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "journal").mkdir(parents=True, exist_ok=True)
        return root

    def as_dict(self) -> dict[str, Any]:
        payload = {
            name: getattr(self, name)
            for name in self.__slots__
        }
        payload["root"] = str(self.root)
        return payload


__all__ = ["Settings", "ENV_PREFIX", "ConfigIssue"]
