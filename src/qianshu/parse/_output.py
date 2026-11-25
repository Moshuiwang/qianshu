"""检查含申请明细的解析产物能否安全落盘。"""

import os
import subprocess
from pathlib import Path

from qianshu.config import ConfigError


def check_output_path(path: Path, *, git_env: dict[str, str] | None = None) -> None:
    """仓库内只允许 Git 忽略且尚未跟踪的路径。"""
    try:
        target = path.resolve()
    except (OSError, RuntimeError):
        raise ConfigError("无法解析输出路径。") from None
    for root in (target, *target.parents):
        metadata = root / ".git"
        # 普通仓库的元数据目录内有 HEAD，关联工作树和子模块可使用 gitfile。
        if metadata.is_file() or (metadata / "HEAD").is_file():
            break
    else:
        return

    relative = target.relative_to(root)
    if ".git" in relative.parts:
        raise ConfigError("输出路径不能位于 Git 元数据目录。")
    # 由输出所在目录定位工作树，使用该工作树的索引。
    if git_env is None:
        git_env = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            git_env.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", "--", f"./{relative}"],
            env=git_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise ConfigError("无法确认输出路径的 Git 忽略状态。") from None
    # check-ignore 默认会排除索引里已有的路径，即使文件已从工作树删除。
    if result.returncode == 1:
        raise ConfigError("输出路径位于 Git 工作区，须已被 Git 忽略且未被跟踪。")
    if result.returncode != 0:
        raise ConfigError("无法确认输出路径的 Git 忽略状态。")
