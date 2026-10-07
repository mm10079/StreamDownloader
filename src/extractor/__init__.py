import importlib
import pkgutil
from typing import Optional

from ._extractor import ExtractContext, ExtractorConfig, InfoExtractor

_ALL_EXTRACTORS: list[InfoExtractor] = []
LOAD_ERRORS: dict[str, str] = {}


def _walk_and_load(package_path: str, package_name: str) -> None:
    """遞迴掃描 adapters/ 底下所有模組，收集啟用中的 InfoExtractor 子類別"""
    for _, module_name, is_pkg in pkgutil.iter_modules([package_path]):
        full_name = f"{package_name}.{module_name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as e:      # 單一網站模組壞掉不影響其他網站
            LOAD_ERRORS[full_name] = f"{type(e).__name__}: {e}"
            continue
        if is_pkg:
            _walk_and_load(module.__path__[0], full_name)
        for attr in vars(module).values():
            if (isinstance(attr, type) and issubclass(attr, InfoExtractor) and attr is not InfoExtractor
                    and attr.__module__ == module.__name__ and attr.config.enable):
                _ALL_EXTRACTORS.append(attr())


def load_extractors() -> list[InfoExtractor]:
    if not _ALL_EXTRACTORS:
        from . import adapters
        _walk_and_load(adapters.__path__[0], adapters.__name__)
        _ALL_EXTRACTORS.sort(key=lambda e: e.config.priority)
    return _ALL_EXTRACTORS


def find_extractor(url: str) -> Optional[InfoExtractor]:
    for extractor in load_extractors():
        if extractor.match(url):
            return extractor
    return None


__all__ = ["InfoExtractor", "ExtractorConfig", "ExtractContext", "load_extractors", "find_extractor", "LOAD_ERRORS"]
