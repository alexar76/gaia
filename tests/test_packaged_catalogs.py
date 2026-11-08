"""A pip-installed gaia reads the catalogs its wheel carries in gaia/_config/.

The modules used to read only the repository's config/ folder, which an installed package does not have, so
importing them raised FileNotFoundError (aimarket-atlas 0.3.5 and aimarket-gaia-gateway 0.2.2 on PyPI). Each case
copies the module into a site-packages-like layout with only the packaged copy and loads it from there.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

TREE = Path(__file__).resolve().parents[1]
CATALOGS = TREE / "config"


def _install_like(tmp_path: Path, module: str) -> Path:
    target = tmp_path / "site-packages" / "gaia" / "devices" / f"{module}.py"
    target.parent.mkdir(parents=True)
    shutil.copy(TREE / "gaia" / "devices" / f"{module}.py", target)
    packaged = tmp_path / "site-packages" / "gaia" / "_config"
    packaged.mkdir(parents=True, exist_ok=True)
    for name in ("om_mesh_cities.yaml", "sc_mesh_cities.yaml", "extra_sensors.yaml"):
        shutil.copy(CATALOGS / name, packaged / name)
    assert not (tmp_path / "site-packages" / "config").exists()
    return target


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("module, constant", [("om_mesh", "OM_MESH_CITIES"), ("sc_mesh", "SC_MESH_CITIES")])
def test_mesh_module_imports_from_the_packaged_copy(tmp_path: Path, module: str, constant: str) -> None:
    mod = _load(_install_like(tmp_path, module), f"installed_gaia_{module}")
    assert len(getattr(mod, constant)) > 0
    assert mod._config_path().parent.name == "_config"


def test_extra_sensors_read_from_the_packaged_copy(tmp_path: Path) -> None:
    mod = _load(_install_like(tmp_path, "extra_sensors"), "installed_gaia_extra_sensors")
    assert mod._config_path().parent.name == "_config"
    assert len(mod.load_sensors()) > 0
