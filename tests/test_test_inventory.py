from __future__ import annotations

from scripts.test_inventory import scan_test_tree


def test_scan_test_tree_reports_size_and_exact_duplicate_bodies(tmp_path) -> None:
    (tmp_path / "test_alpha.py").write_text(
        "def test_first():\n"
        "    value = 1\n"
        "    assert value == 1\n\n"
        "def test_unique():\n"
        "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    (tmp_path / "test_beta.py").write_text(
        "def test_second():\n"
        "    value = 1\n"
        "    assert value == 1\n",
        encoding="utf-8",
    )

    inventory = scan_test_tree(tmp_path)

    assert inventory.file_count == 2
    assert inventory.function_count == 3
    assert inventory.source_lines == 9
    assert len(inventory.exact_duplicate_groups) == 1
    locations = inventory.exact_duplicate_groups[0]
    assert {(item.path.name, item.line, item.name) for item in locations} == {
        ("test_alpha.py", 1, "test_first"),
        ("test_beta.py", 1, "test_second"),
    }
