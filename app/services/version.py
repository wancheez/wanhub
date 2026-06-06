"""Версия бота для /device и логов.

Семантическая версия (`APP_VERSION`) задаётся вручную в конфиге, а точную
идентификацию сборки даёт git: короткий хэш коммита + дата + ветка. Деплой
идёт через `git pull` + рестарт сервиса, поэтому git-коммит — источник правды
о том, какой именно код реально крутится на машине.

Считаем один раз и кэшируем: процесс перезапускается при деплое, так что
закэшированное значение остаётся актуальным на всё время жизни процесса.
Если git недоступен (нет бинаря или это не git-checkout) — читаем `.git`
напрямую, а при полном провале отдаём только семантическую версию.
"""

import subprocess
from dataclasses import dataclass

from app.core.config import APP_VERSION, PROJECT_ROOT


@dataclass(frozen=True)
class VersionInfo:
    app_version: str
    commit: str | None  # короткий sha, напр. 'fc7082e'
    commit_date: str | None  # 'YYYY-MM-DD'
    branch: str | None
    dirty: bool  # есть ли незакоммиченные изменения в рабочем дереве

    def short(self) -> str:
        """Однострочная версия для /device и логов."""
        if self.commit is None:
            return self.app_version
        parts = [f"{self.app_version}+{self.commit}"]
        if self.commit_date:
            parts.append(f"({self.commit_date})")
        if self.branch and self.branch != "main":
            parts.append(f"[{self.branch}]")
        if self.dirty:
            parts.append("dirty")
        return " ".join(parts)


def _git(*args: str) -> str | None:
    """Прогнать git в каталоге проекта. None при любой ошибке/недоступности.

    GIT_OPTIONAL_LOCKS=0 — чтобы read-команды не пытались писать в .git
    (важно под systemd с ProtectHome=read-only).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=2,
            env={"GIT_OPTIONAL_LOCKS": "0", "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _read_git_fallback() -> tuple[str | None, str | None]:
    """(short_sha, branch) чтением .git напрямую, если бинаря git нет."""
    try:
        head = (PROJECT_ROOT / ".git" / "HEAD").read_text().strip()
    except OSError:
        return None, None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        branch = ref.rsplit("/", 1)[-1]
        try:
            sha = (PROJECT_ROOT / ".git" / ref).read_text().strip()
        except OSError:
            return None, branch
        return sha[:7], branch
    # detached HEAD — в HEAD лежит сам sha
    return head[:7], None


def _compute() -> VersionInfo:
    commit = _git("rev-parse", "--short", "HEAD")
    commit_date = _git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    # `git status --porcelain` непустой → есть незакоммиченные изменения.
    dirty = bool(_git("status", "--porcelain"))

    if commit is None:
        commit, branch = _read_git_fallback()
        dirty = False  # без git точно сказать нельзя — не пугаем

    return VersionInfo(
        app_version=APP_VERSION,
        commit=commit,
        commit_date=commit_date,
        branch=branch,
        dirty=dirty,
    )


_cached: VersionInfo | None = None


def get_version() -> VersionInfo:
    global _cached
    if _cached is None:
        _cached = _compute()
    return _cached
