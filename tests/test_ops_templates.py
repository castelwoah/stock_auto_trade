"""ops 템플릿: 하드코딩 경로 재유입 방지 + 렌더 결과에 placeholder 가 남지 않는지."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted(p for p in (ROOT / "ops").rglob("*") if p.is_file())
KNOWN = {"@PROJECT_ROOT@", "@RUN_USER@", "@RUN_GROUP@", "@PYTHON@"}


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_hardcoded_home_paths(path):
    text = path.read_text(encoding="utf-8")
    assert "/home/" not in text and "miniconda" not in text


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_render_leaves_no_placeholders(path, tmp_path):
    out = tmp_path / path.name
    proc = subprocess.run(
        ["bash", "-c", f'source "{ROOT}/scripts/_ops_common.sh" && render "{path}" "{out}"'],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    rendered = out.read_text(encoding="utf-8")
    assert not any(k in rendered for k in KNOWN)
    assert str(ROOT) in rendered
