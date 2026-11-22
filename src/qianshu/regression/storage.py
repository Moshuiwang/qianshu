"""本地回归产物的 Git 忽略检查和私有文件访问。"""

import json
import os
import stat
import subprocess
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path


class RegressionError(ValueError):
    """可显示的回归工具错误。消息只能含预定义文字、计数和字段名。"""


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def json_bytes(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _unique_keys(items: list[tuple[str, object]]) -> dict:
    result = dict(items)
    if len(result) != len(items):
        raise RegressionError("私有 JSON 含重复键。")
    return result


def _invalid_constant(_: str) -> None:
    raise RegressionError("私有 JSON 含无效数值。")


def read_json(raw: bytes, **kwargs) -> object:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant, **kwargs)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )


def _relative(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RegressionError("私有资料路径须为 .private 内的相对路径。")
    return path


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_INPUT = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class PrivateFiles:
    """相对仓库根的 .private 目录。通过目录描述符打开路径的每一层。"""

    def __init__(self, repo: Path):
        self.repo = repo.resolve(strict=True)

    def _in_worktree(self, directory: Path) -> bool:
        top = _git(directory, "rev-parse", "--show-toplevel")
        return top.returncode == 0 and Path(os.fsdecode(top.stdout.rstrip(b"\n"))) == self.repo

    @contextmanager
    def open(self):
        if not self._in_worktree(self.repo):
            raise RegressionError("请在 Git 工作树的根目录运行。")
        tracked = _git(self.repo, "ls-files", "-z", "--", ".private")
        if tracked.returncode or tracked.stdout:
            raise RegressionError(".private 包含 Git 跟踪的路径或无法检查 Git 索引。")
        self._check_ignore(Path(".private"), directory=True)
        root = os.open(self.repo / ".private", _DIR)
        try:
            info = os.fstat(root)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise RegressionError(".private 必须由当前用户所有，且仅允许当前用户访问。")
            self.root = root
            yield self
        finally:
            os.close(root)

    def _check_ignore(self, path: Path, *, directory: bool = False) -> None:
        name = path.as_posix() + ("/" if directory else "")
        result = _git(self.repo, "check-ignore", "--no-index", "-q", "--", name)
        if result.returncode:
            raise RegressionError("私有资料与输出必须位于 Git 忽略的 .private 路径内。")

    def argument(self, value: str | Path, *, allow_root: bool = False) -> Path:
        path = _relative(value)
        if path.parts[0] != ".private" or (len(path.parts) == 1 and not allow_root):
            raise RegressionError("请指定 .private 下的文件或子目录。")
        return Path(*path.parts[1:])

    @contextmanager
    def _directory(self, path: Path):
        current = os.dup(self.root)
        try:
            for part in path.parts:
                child = os.open(part, _DIR, dir_fd=current)
                os.close(current)
                current = child
            yield current
        finally:
            os.close(current)

    def read(self, path: str | Path) -> bytes:
        path = _relative(path)
        self._check_ignore(Path(".private") / path)
        with self._directory(path.parent) as parent:
            fd = os.open(path.name, _INPUT, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RegressionError("私有资料必须是普通文件且没有硬链接。")
                return stream.read()

    @contextmanager
    def new_output(self, path: Path, names: tuple[str, ...]):
        path = _relative(path)
        target = Path(".private") / path
        self._check_ignore(target, directory=True)
        for name in names:
            self._check_ignore(target / name)
        with self._directory(path.parent) as parent:
            if not self._in_worktree((self.repo / target).parent):
                raise RegressionError("私有输出必须位于当前 Git 工作树中。")
            os.mkdir(path.name, mode=0o700, dir_fd=parent)
            folder = os.open(path.name, _DIR, dir_fd=parent)
            try:
                yield Output(self, path, folder, names)
            finally:
                os.close(folder)


class Output:
    def __init__(self, files: PrivateFiles, path: Path, folder: int, names: tuple[str, ...]):
        self.files = files
        self.path = path
        self.folder = folder
        self.names = names
        self.durable = False

    def write(self, name: str, data: bytes) -> None:
        if name not in self.names:
            raise RegressionError("输出文件未通过路径检查。")
        self.files._check_ignore(Path(".private") / self.path / name)
        root_entry = self.files.repo / ".private"
        with self.files._directory(self.path) as checked:
            if not (
                os.path.samestat(root_entry.lstat(), os.fstat(self.files.root))
                and os.path.samestat(os.fstat(checked), os.fstat(self.folder))
            ):
                raise RegressionError("私有输出目录在运行期间发生变化。")
            if not self.files._in_worktree(root_entry / self.path):
                raise RegressionError("私有输出必须位于当前 Git 工作树中。")
        fd = os.open(name, _NEW, mode=0o600, dir_fd=self.folder)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            if self.durable:
                stream.flush()
                os.fsync(stream.fileno())


@contextmanager
def staged_output(files, path, names):
    """批次可重复取回；只有含完整清单的目录才能占用最终输出名。"""
    from uuid import uuid4
    stage = path.with_name(".regression-" + uuid4().hex)
    with files.new_output(stage, names) as output:
        output.durable = True
        yield output
        os.fsync(output.folder)
        files._check_ignore(Path(".private") / path, directory=True)
        for name in names:
            files._check_ignore(Path(".private") / path / name)
        with files._directory(path.parent) as parent, files._directory(stage) as checked:
            if not os.path.samestat(os.fstat(checked), os.fstat(output.folder)):
                raise RegressionError("批量输出暂存目录已变更。")
            os.rename(stage.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
