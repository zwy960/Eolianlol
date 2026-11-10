"""体检结论的数据结构：Finding / Report / SafeRefusal。

分级只有四档，含义固定：

    ok      通过（也用来记录"已做过的动作"）
    info    事实记录，不影响决策
    warn    可疑但不致命（策略 balanced 下放行，strict 下可升级为 block）
    block   硬失败：调用方必须放弃注入/清理失败必须报错
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

LEVELS = ("ok", "info", "warn", "block")
_RANK = {"ok": 0, "info": 1, "warn": 2, "block": 3}


class SafeRefusal(RuntimeError):
    """门禁拒绝：携带 Report，便于上层显示完整原因。"""

    def __init__(self, message: str, report: "Report | None" = None):
        super().__init__(message)
        self.report = report


@dataclass
class Finding:
    level: str
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in _RANK:
            raise ValueError(f"未知等级: {self.level!r}")

    @property
    def rank(self) -> int:
        return _RANK[self.level]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "detail": dict(self.detail),
        }


@dataclass
class Report:
    """一次体检/一次动作的结论集合。"""

    scope: str
    findings: list[Finding] = field(default_factory=list)

    # ---- 写入 ----

    def add(self, level: str, code: str, message: str, **detail: Any) -> Finding:
        finding = Finding(level, code, message, detail)
        self.findings.append(finding)
        return finding

    def ok(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("ok", code, message, **detail)

    def info(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("info", code, message, **detail)

    def warn(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("warn", code, message, **detail)

    def block(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("block", code, message, **detail)

    def merge(self, other: "Report") -> "Report":
        self.findings.extend(other.findings)
        return self

    # ---- 读取 ----

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "block"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def passed(self) -> bool:
        return not self.blocking

    def counts(self) -> dict[str, int]:
        out = {level: 0 for level in LEVELS}
        for finding in self.findings:
            out[finding.level] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "passed": self.passed,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
        }

    def format(self, *, verbose: bool = False) -> str:
        marks = {"ok": "[ ok ]", "info": "[info]", "warn": "[warn]", "block": "[BLOCK]"}
        lines = [f"== {self.scope} =="]
        for finding in self.findings:
            if not verbose and finding.level in ("ok", "info"):
                continue
            lines.append(f"{marks[finding.level]} {finding.code}  {finding.message}")
            if verbose and finding.detail:
                pairs = " ".join(f"{k}={v}" for k, v in finding.detail.items())
                lines.append(f"         {pairs}")
        c = self.counts()
        lines.append(
            f"-- 结果: {'通过' if self.passed else '拦截'}  "
            f"block={c['block']} warn={c['warn']} info={c['info']} ok={c['ok']}"
        )
        return "\n".join(lines)

    def raise_if_blocking(self, context: str = "") -> None:
        if not self.blocking:
            return
        first = self.blocking[0]
        reason = first.message
        if len(self.blocking) > 1:
            reason += f"（另有 {len(self.blocking) - 1} 项拦截）"
        prefix = f"{context}: " if context else ""
        raise SafeRefusal(f"{prefix}{reason}", self)

    def short_reason(self, *, limit: int = 120) -> str:
        """给 UI 单行提示用：``CODE 原因``，多项时带总数。"""
        blocks = self.blocking
        if not blocks:
            return ""
        first = blocks[0]
        text = f"{first.code} {first.message}"
        if len(blocks) > 1:
            text += f"（共 {len(blocks)} 项拦截）"
        return text if len(text) <= limit else text[: limit - 1] + "…"


def worst(findings: Iterable[Finding]) -> str:
    level = "ok"
    for finding in findings:
        if _RANK[finding.level] > _RANK[level]:
            level = finding.level
    return level
