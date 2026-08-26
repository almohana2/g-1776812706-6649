"""حارس القراءة فقط: لا يوجد في المستودع أي استدعاء لأوامر التشغيل.

SRS §5.4 و§19 يمنعان ``setzone.php`` منعًا باتًا. الاختبار يفحص الشجرة
كلها لا وحدة بعينها، لأن الخطر الحقيقي أن تُضاف النقطة لاحقًا في مكان آخر.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("app", "worker", "scripts", "migrations")
FORBIDDEN = re.compile(r"setzone|stopall|runall|suspendall", re.IGNORECASE)


def source_files() -> list[Path]:
    files: list[Path] = []
    for folder in SCANNED:
        files.extend(
            path
            for path in (ROOT / folder).rglob("*.py")
            if "__pycache__" not in path.parts
        )
        files.extend((ROOT / folder).rglob("*.html"))
    return files


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_zone_command_endpoint_anywhere(path: Path):
    text = path.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if FORBIDDEN.search(line):
            # يُسمح بذكرها في تعليق يشرح المنع، لا في كود يستدعيها.
            stripped = line.strip()
            is_comment = stripped.startswith("#") or stripped.startswith("*")
            in_docstring_or_prose = "غير" in line or "لا " in line or "منع" in line
            assert is_comment or in_docstring_or_prose, (
                f"{path.relative_to(ROOT)}:{number} تذكر أمر تشغيل محبس: {stripped}"
            )


def test_no_control_button_in_templates():
    for path in (ROOT / "app" / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "تشغيل المحبس" not in text
        assert "إيقاف المحبس" not in text
