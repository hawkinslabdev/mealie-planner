"""Locale tests. Validates against primary language `en.json`:
- No missing translations
- No orphaned translations
- Placeholder variables preserved
- Valid JSON structure
- All locale files registered in app.js + main.py
"""

import json
import re
from pathlib import Path

LOCALES_DIR = Path(__file__).parent.parent / "assets" / "locales"
ROOT = LOCALES_DIR.parent.parent
REFERENCE = "en.json"


def _locale_codes_on_disk() -> set[str]:
    return {f.stem for f in LOCALES_DIR.glob("*.json") if f.stem != "en"}


def _parse_js_array(name: str, text: str) -> set[str]:
    m = re.search(rf"const\s+{name}\s*=\s*\[([^\]]+)\]", text)
    assert m, f"{name} not found in app.js"
    return {s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip().strip("'\"")}


def _parse_js_object_keys(name: str, text: str) -> set[str]:
    m = re.search(rf"const\s+{name}\s*=\s*\{{([^}}]+)\}}", text)
    assert m, f"{name} not found in app.js"
    return {s.split(":")[0].strip().strip("'\"") for s in m.group(1).split(",") if ":" in s}


def _parse_py_set(name: str, text: str) -> set[str]:
    m = re.search(rf"{name}\s*=\s*\{{([^}}]+)\}}", text)
    assert m, f"{name} not found in main.py"
    return {s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip().strip("'\"")}


def load(filename: str) -> dict:
    return json.loads((LOCALES_DIR / filename).read_text(encoding="utf-8"))


def flat_keys(d: dict, prefix: str = "") -> dict[str, str]:
    """Flatten nested locale dict to dotted-key → value map."""
    result = {}
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(flat_keys(v, full))
        else:
            result[full] = str(v)
    return result


def placeholders(value: str) -> set[str]:
    return set(re.findall(r"\{[^}]+\}", value))


def locale_files() -> list[str]:
    return [f.name for f in sorted(LOCALES_DIR.glob("*.json")) if f.name != REFERENCE]

# Parametrize over every non-English locale file
import pytest


@pytest.fixture(scope="module")
def reference() -> dict[str, str]:
    return flat_keys(load(REFERENCE))


@pytest.mark.parametrize("locale", locale_files())
class TestLocaleCompleteness:
    def test_no_missing_keys(self, locale: str, reference: dict[str, str]):
        """Every key in en.json must exist in the locale."""
        translated = flat_keys(load(locale))
        missing = sorted(set(reference) - set(translated))
        assert not missing, (
            f"{locale} missing {len(missing)} key(s):\n" + "\n".join(f"  {k}" for k in missing)
        )

    def test_no_extra_keys(self, locale: str, reference: dict[str, str]):
        """Locale must not have keys absent from en.json (stale/orphaned)."""
        translated = flat_keys(load(locale))
        extra = sorted(set(translated) - set(reference))
        assert not extra, (
            f"{locale} has {len(extra)} extra key(s) not in en.json:\n"
            + "\n".join(f"  {k}" for k in extra)
        )

    def test_placeholders_preserved(self, locale: str, reference: dict[str, str]):
        """Placeholder variables like {n}, {name}, {detail} must survive translation."""
        translated = flat_keys(load(locale))
        mismatches: list[str] = []
        for key, en_val in reference.items():
            expected = placeholders(en_val)
            if not expected:
                continue
            actual = placeholders(translated.get(key, ""))
            if expected != actual:
                mismatches.append(
                    f"  {key}: expected {sorted(expected)}, got {sorted(actual)}"
                )
        assert not mismatches, (
            f"{locale} placeholder mismatch in {len(mismatches)} key(s):\n"
            + "\n".join(mismatches)
        )

    def test_no_empty_values(self, locale: str):
        """No key should have an empty string (likely untranslated placeholder)."""
        translated = flat_keys(load(locale))
        empty = sorted(k for k, v in translated.items() if v.strip() == "")
        assert not empty, (
            f"{locale} has {len(empty)} empty value(s):\n" + "\n".join(f"  {k}" for k in empty)
        )


class TestLocaleRegistration:
    """Every locale file on disk must be registered in all code-level lists."""

    @pytest.fixture(scope="class")
    def app_js(self) -> str:
        return (ROOT / "assets" / "app.js").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def main_py(self) -> str:
        return (ROOT / "main.py").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def index_html(self) -> str:
        return (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    def test_app_js_supported_locales(self, app_js: str):
        disk = _locale_codes_on_disk()
        registered = _parse_js_array("SUPPORTED_LOCALES", app_js) - {"en"}
        missing = disk - registered
        assert not missing, f"app.js SUPPORTED_LOCALES missing: {sorted(missing)}"

    def test_app_js_locale_names(self, app_js: str):
        disk = _locale_codes_on_disk()
        registered = _parse_js_object_keys("LOCALE_NAMES", app_js) - {"en"}
        missing = disk - registered
        assert not missing, f"app.js LOCALE_NAMES missing: {sorted(missing)}"

    def test_main_py_supported_locales(self, main_py: str):
        disk = _locale_codes_on_disk()
        registered = _parse_py_set("_SUPPORTED_LOCALES", main_py) - {"en"}
        missing = disk - registered
        assert not missing, f"main.py _SUPPORTED_LOCALES missing: {sorted(missing)}"

    def test_no_hardcoded_locale_options_in_template(self, index_html: str):
        """Template must use x-for, not hardcoded <option> tags per locale."""
        hardcoded = re.findall(r'<option\s+value=["\'][a-z]{2}["\']', index_html)
        assert not hardcoded, (
            f"index.html has {len(hardcoded)} hardcoded locale <option> tag(s) — use x-for instead"
        )


class TestReferenceLocale:
    def test_en_valid_json(self):
        data = load(REFERENCE)
        assert isinstance(data, dict)

    def test_en_no_empty_values(self):
        flat = flat_keys(load(REFERENCE))
        empty = sorted(k for k, v in flat.items() if v.strip() == "")
        assert not empty, f"en.json has empty values: {empty}"

    def test_all_locale_files_valid_json(self):
        errors = []
        for f in LOCALES_DIR.glob("*.json"):
            try:
                json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                errors.append(f"{f.name}: {e}")
        assert not errors, "Invalid JSON:\n" + "\n".join(errors)
