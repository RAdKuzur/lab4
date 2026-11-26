"""Тесты политики маскирования ``masking``.

Проверяют удаление и маскирование атрибутов без мутации исходного
спана, поведение при пустых правилах, загрузку правил из файла и
возврат дефолтов при отсутствии файла.
"""
from pathlib import Path
from typing import Any

from src.policy import masking


def test_apply_drop_and_mask(sample_span: dict[str, Any]) -> None:
    """Проверяет удаление и маскирование атрибутов без мутации исходника.

    Ключ из ``drop_keys`` удаляется, ключ из ``mask_keys`` заменяется
    на ``mask_value``, прочие атрибуты сохраняются; исходный спан
    остаётся неизменным.

    Args:
        sample_span: Эталонный спан из общей фикстуры.
    """
    rules = {
        "drop_keys": ["http.request.header.authorization"],
        "mask_keys": ["user.email"],
        "mask_value": "***",
    }
    span = dict(sample_span)
    span["attributes"] = {
        "http.request.header.authorization": "Bearer xxx",
        "user.email": "ivan@example.com",
        "http.method": "GET",
    }
    out = masking.apply(span, rules)
    assert "http.request.header.authorization" not in out["attributes"]
    assert out["attributes"]["user.email"] == "***"
    assert out["attributes"]["http.method"] == "GET"
    assert "user.email" in span["attributes"]  # исходный не изменён


def test_apply_no_rules(sample_span: dict[str, Any]) -> None:
    """Проверяет, что при пустых правилах атрибуты не меняются.

    Args:
        sample_span: Эталонный спан из общей фикстуры.
    """
    out = masking.apply(sample_span, {"drop_keys": [], "mask_keys": []})
    assert out["attributes"] == sample_span["attributes"]


def test_load_rules_missing_file(tmp_path: Path) -> None:
    """Проверяет возврат ``DEFAULT_RULES`` при отсутствии файла правил.

    Args:
        tmp_path: Временный каталог, предоставляемый pytest.
    """
    rules = masking.load_rules(str(tmp_path / "nonexistent.json"))
    assert rules == masking.DEFAULT_RULES


def test_load_rules_from_file(tmp_path: Path) -> None:
    """Проверяет загрузку правил из существующего JSON-файла.

    Args:
        tmp_path: Временный каталог, предоставляемый pytest.
    """
    import json
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"drop_keys": ["a"], "mask_keys": ["b"], "mask_value": "X"}))
    rules = masking.load_rules(str(p))
    assert rules["drop_keys"] == ["a"]
    assert rules["mask_value"] == "X"