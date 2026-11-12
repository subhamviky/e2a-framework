"""v1.2.0 package layout (E-12): the e2a package is the framework; e2a_base stays a
compatibility module with every v1.1.x name."""
import importlib
import pkgutil
from pathlib import Path

import e2a
import e2a_base

NAMES = (Path(__file__).parent / "data" / "e2a_base_v1_1_names.txt").read_text().split()


def test_every_v1_1_name_is_still_importable_from_e2a_base():
    assert [n for n in NAMES if not hasattr(e2a_base, n)] == []


def test_e2a_base_re_exports_the_same_objects():
    for name in ("BaseAgent", "BaseWorkflow", "BaseRAGPipeline", "BaseToolService", "resolve_config",
                 "PhysicalOutcome", "run_sync"):
        assert getattr(e2a_base, name) is getattr(e2a, name)


def test_star_import_still_provides_the_v1_1_names():
    namespace = {}
    exec("from e2a_base import *", namespace)
    assert {"BaseAgent", "resolve_config", "asyncio", "Dict"} <= namespace.keys()


def test_every_module_imports_on_its_own():
    for module in pkgutil.iter_modules(e2a.__path__):
        importlib.import_module(f"e2a.{module.name}")


def test_public_names_have_one_home():
    homes = {}
    for module in pkgutil.iter_modules(e2a.__path__):
        mod = importlib.import_module(f"e2a.{module.name}")
        for name, value in vars(mod).items():
            if not name.startswith("_") and getattr(value, "__module__", None) == mod.__name__:
                assert name not in homes, f"{name} defined in {homes[name]} and {mod.__name__}"
                homes[name] = mod.__name__
    assert homes["BaseAgent"] == "e2a.agents" and homes["BaseWorkflow"] == "e2a.workflows"
