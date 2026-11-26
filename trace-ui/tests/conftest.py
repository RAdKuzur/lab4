"""Инициализация тестового окружения для набора тестов.

Добавляет корень проекта в ``sys.path``, чтобы тесты могли импортировать
пакеты ``src`` и ``auth``, и выставляет ``SESSION_SECRET`` по умолчанию,
если он не задан в окружении.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("SESSION_SECRET", "test-secret")