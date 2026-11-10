"""日志脱敏。

日志是最诚实的自证材料：``runtime.log`` 里那句

    patching module: 0000000140000000 CRYPTO_free offset: 0000000001CABDE0

把模块基址和补丁偏移一起写进了磁盘，等于每次版本变动都给对照表。
这里提供最小可用的一套规则：地址、绝对路径、账号、哈希、pid/tid 全部打码，
但保留行结构与错误类型，方便排障。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SEP = r"(?:\\{1,2}|/)"

# 顺序很重要：先打码长串，再打码地址，最后处理路径
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9A-Fa-f]{32,128}\b"), "<hash>"),
    # 裸十六进制（至少含一个 a-f）：补丁偏移经常不带 0x 前缀
    (re.compile(r"\b(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{8,31}\b"), "<hex>"),
    (re.compile(r"\b0x[0-9A-Fa-f]{4,16}\b"), "0x…"),
    # 绝对路径：兼容 Windows 反斜杠、转义后的双反斜杠（repr）、以及正斜杠写法
    (re.compile(
        r"\b[A-Za-z]:" + _SEP
        + r"(?:[^\\/\s\"'|*?<>]+" + _SEP + r")*[^\\/\s\"'|*?<>]*"
    ), "<path>"),
    (re.compile(r"\bpid[=:]\s*\d+", re.IGNORECASE), "pid=<pid>"),
    (re.compile(r"\btid[=:]\s*\d+", re.IGNORECASE), "tid=<tid>"),
    # 没有盘符的路径碎片（历史日志被部分打码后留下的 \Users\\xxx\yyy）
    (re.compile(r"(?:\\{1,2}[^\\/\s\"'|*?<>]{2,}){2,}"), "<path>"),
    # 账号 / QQ：9 位以上纯数字；八位日期（20261106）不在此列
    (re.compile(r"\b\d{9,}\b"), "<id>"),
)


def redact_line(line: str) -> str:
    out = line
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_text(text: str, *, drop_debug_lines: bool = True) -> str:
    """逐行脱敏；``drop_debug_lines`` 会丢弃 ``[DLL] debug: open`` 这类噪声行。"""
    out: list[str] = []
    for line in text.splitlines():
        if drop_debug_lines and ("[DLL] debug:" in line or "debug: open:" in line):
            continue
        out.append(redact_line(line))
    return "\n".join(out)


@dataclass
class RedactResult:
    path: Path
    lines: int
    changed: bool
    error: str | None = None


def sanitize_file(path: str | Path, *, keep_backup: bool = False) -> RedactResult:
    target = Path(path)
    if not target.is_file():
        return RedactResult(target, 0, False, "文件不存在")
    try:
        original = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return RedactResult(target, 0, False, str(exc))
    cleaned = redact_text(original)
    if cleaned == original:
        return RedactResult(target, len(original.splitlines()), False)
    if keep_backup:
        backup = target.with_suffix(target.suffix + ".raw")
        try:
            if not backup.exists():
                backup.write_text(original, encoding="utf-8")
        except OSError:
            pass
    try:
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(cleaned, encoding="utf-8")
        temp.replace(target)
    except OSError:
        # 目标被其他进程（或 diag 自己的追加句柄）占着时，rename 需要 DELETE 权限会失败；
        # 但写权限通常是共享的 —— 退化成原地截断重写。
        try:
            with open(target, "w", encoding="utf-8", errors="replace") as handle:
                handle.write(cleaned)
            temp = target.with_suffix(target.suffix + ".tmp")
            if temp.exists():
                temp.unlink(missing_ok=True)
        except OSError as exc:
            return RedactResult(target, len(original.splitlines()), False, str(exc))
    return RedactResult(target, len(cleaned.splitlines()), True)
