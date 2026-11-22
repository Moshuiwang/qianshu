"""Exercise CI entry points against synthetic repositories and manifests."""

from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def write(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic\n")


def commit():
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
         "commit", "-qm", "fixture", "--allow-empty"],
        check=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def level(base):
    return subprocess.run(
        [sys.executable, SCRIPTS / "ci_level.py", base, "HEAD"],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize(("before", "after", "expected"), [
    ("src/module.py", None, "full"),
    ("src/module.py", "docs/module.md", "full"),
    ("docs/module.md", "src/module.py", "full"),
    ("docs/旧 名.md", "docs/新\n名.md", "docs"),
    (None, "docs/中文.md", "docs"),
    (None, "README.md", "docs"),
    (None, "README.md\ndocs/fake", "full"),
    (None, None, "full"),
])
def test_paths(repo, before, after, expected):
    if before:
        write(repo / before)
    base = commit()
    if before and after:
        (repo / after).parent.mkdir(parents=True, exist_ok=True)
        (repo / before).rename(repo / after)
    elif before:
        (repo / before).unlink()
    elif after:
        write(repo / after)
    commit()
    result = level(base)
    result.check_returncode()
    assert result.stdout.strip() == expected


def guard(base, proposed):
    return subprocess.run(
        ["sh", SCRIPTS / "ci_guard.sh", base, "HEAD", proposed],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize("path", [
    "scripts/ci_level.py", "scripts/ci_guard.sh", ".github/pull_request_template.md",
    ".github/workflows/checks.yml", "pyproject.toml", "uv.lock",
])
def test_guard_overrides_docs(repo, path):
    write(repo / path)
    base = commit()
    (repo / "docs").mkdir()
    (repo / path).rename(repo / "docs/moved.md")
    commit()
    result = guard(base, "docs")
    result.check_returncode()
    assert result.stdout == "full\n"


@pytest.mark.parametrize("proposed", ["", "unknown", "docs\nfull"])
def test_guard_rejects_invalid_level_even_for_protected_path(repo, proposed):
    base = commit()
    write(repo / "uv.lock")
    commit()
    assert guard(base, proposed).returncode != 0


def test_guard_baselines(repo):
    commit()
    manual = guard("", "docs")
    manual.check_returncode()
    assert manual.stdout == "full\n"
    assert guard("unknown-revision", "docs").returncode != 0


def test_diverged_base_and_head(repo):
    ancestor = commit()
    write(repo / "scripts/ci_base_only.py")
    base = commit()
    subprocess.run(["git", "checkout", "-q", "--detach", ancestor], check=True)
    write(repo / "docs/pr.md")
    commit()

    # A tip-to-tip diff also sees the base-only CI script as deleted.
    for result in (level(base), guard(base, "docs")):
        result.check_returncode()
        assert result.stdout == "docs\n"


def test_large_mixed_diff(repo):
    base = commit()
    write(repo / "code.py")
    for i in range(1500):
        write(repo / "docs" / f"{i:04}-{'x' * 80}.md")
    commit()
    result = level(base)
    result.check_returncode()
    assert result.stdout.strip() == "full"


def test_baselines(repo):
    commit()
    manual = level("")
    manual.check_returncode()
    assert manual.stdout.strip() == "full"
    assert level("unknown-revision").returncode != 0


@pytest.mark.parametrize("case", ["current", "missing-project", "missing-lock", "drift"])
def test_lock(repo, monkeypatch, case):
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(repo.parent / (repo.name + "-uv-cache")))
    (repo / ".python-version").write_text("3.12.13\n")
    project = repo / "pyproject.toml"
    wheels = repo / "wheels"
    wheels.mkdir()
    for version in ("1.0", "2.0"):
        info = f"fixture_dep-{version}.dist-info"
        with ZipFile(wheels / f"fixture_dep-{version}-py3-none-any.whl", "w") as wheel:
            wheel.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: fixture-dep\nVersion: {version}\n")
            wheel.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            wheel.writestr(f"{info}/RECORD", "")
    manifest = (
        '[project]\nname = "example"\nversion = "1.0"\nrequires-python = "==3.12.*"\n'
        'dependencies = ["fixture-dep==1.0"]\n'
        '[tool.uv]\nno-index = true\nfind-links = ["wheels"]\n'
    )
    project.write_text(manifest)
    subprocess.run(["uv", "lock", "--managed-python"], check=True)
    lock = repo / "uv.lock"
    original = lock.read_bytes()
    base = commit()
    if case == "missing-project":
        project.unlink()
    elif case == "missing-lock":
        lock.unlink()
    elif case == "drift":
        project.write_text(manifest.replace("fixture-dep==1.0", "fixture-dep==2.0"))
    if case != "current":
        commit()
        tier = level(base)
        tier.check_returncode()
        assert tier.stdout.strip() == "full"

    result = subprocess.run(["sh", SCRIPTS / "ci_check_lock.sh"], capture_output=True, text=True)
    assert (result.returncode == 0) == (case == "current"), result.stderr
    if case == "drift":
        assert "lockfile" in result.stderr and "--check" in result.stderr
    if case == "missing-lock":
        assert not lock.exists()
    else:
        assert lock.read_bytes() == original
