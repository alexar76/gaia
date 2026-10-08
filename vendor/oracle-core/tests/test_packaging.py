"""Every oracle must depend on the shared library by the name this repo actually builds.

On 2026-07-28 all 17 oracles declared `dependencies = ["oracle-core"]`, unpinned. That name
belongs to an unrelated project on PyPI which installs the same top-level `oracle_core`
module, so `pip install oracles/chronos` in a clean environment resolved it from the index
and installed a stranger's package — flask, pandas and scikit-learn came with it — ending in
`ImportError: cannot import name 'Capability' from 'oracle_core'`. The family image never
noticed: it installs core and every oracle in one pip invocation, where the local project
satisfies the requirement before the index is consulted.

That is the whole failure mode — a dependency name nothing in the repo provides is not an
error at declaration time, only at install time, and only outside the one environment we
routinely build. So the check is static and needs no network: the name the oracles ask for
must be the name core publishes.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ORACLES = pathlib.Path(__file__).resolve().parents[2] / "oracles"
CORE_PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"

_NAME = re.compile(r'^\s*name\s*=\s*["\']([^"\']+)["\']', re.M)
_DEPS = re.compile(r"^dependencies\s*=\s*\[(.*?)\]", re.M | re.S)
# "aimarket-oracle-core>=0.2" -> "aimarket-oracle-core"
_REQ = re.compile(r'["\']\s*([A-Za-z0-9._-]+)')


def _dist_name(pyproject: pathlib.Path) -> str:
    m = _NAME.search(pyproject.read_text())
    assert m, f"{pyproject} declares no [project] name"
    return m.group(1).lower().replace("_", "-")


def _dependency_names(pyproject: pathlib.Path) -> list[str]:
    m = _DEPS.search(pyproject.read_text())
    if not m:
        return []
    return [r.lower().replace("_", "-") for r in _REQ.findall(m.group(1))]


CORE_DIST = _dist_name(CORE_PYPROJECT)
ORACLE_PYPROJECTS = sorted(ORACLES.glob("*/pyproject.toml"))

#: Repository root, for the dependency-following sweep below.
REPO = pathlib.Path(__file__).resolve().parents[3]
_SKIP_DIRS = {".venv", "venv", "node_modules", "site-packages", ".git", "build", "dist", "__pycache__"}


def _core_consumers() -> list[pathlib.Path]:
    """Every pyproject in the repo that declares a dependency on core, found by FOLLOWING the
    dependency rather than by guessing where such packages live.

    A glob is the wrong instrument here and has now been wrong twice for the same package:
    ``oracles/*/pyproject.toml`` missed ``gaia/`` (which sits outside the oracle tree) during the
    original dependency-name sweep, and missed it again when this guard was written — while also
    missing ``oracles/oracles/platon/backend`` one level deeper than the glob reaches. Anything
    that builds against core is in scope for core's own packaging rules, wherever it happens to
    be checked out, and the dependency edge says so without anyone having to remember.
    """
    found: list[pathlib.Path] = []
    for path in REPO.rglob("pyproject.toml"):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            if CORE_DIST in _dependency_names(path) and path != CORE_PYPROJECT:
                found.append(path)
        except AssertionError:
            continue  # no [project] name — not a distribution we publish
    return sorted(found)


CORE_CONSUMERS = _core_consumers()


def _oracle_tree_dists() -> list[pathlib.Path]:
    """Every publishable distribution under the oracle tree, at ANY depth.

    Following the core dependency (above) catches everything built on oracle_core, but it misses
    a package that lives in the family and does not use core — which is exactly one package, and
    exactly the one that most needed checking. `aimarket-platon` is declared twice, at
    ``oracles/oracles/platon/backend`` and ``platon/backend``; it depends on fastapi and numpy,
    not on core, and it sits one directory deeper than ``oracles/oracles/*/pyproject.toml``
    reaches. So both the glob and the dependency sweep skipped it, and it is the only member of
    the oracle family whose dist name is not ``aimarket-oracle-*``.

    Three different selectors have now each missed platon once. The lesson is not to add a fourth
    special case but to stop selecting by shape: walk the tree.
    """
    found: list[pathlib.Path] = []
    for path in ORACLES.rglob("pyproject.toml"):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            _dist_name(path)
        except AssertionError:
            continue  # no [project] name — not a distribution
        found.append(path)
    return sorted(found)


ORACLE_TREE_DISTS = _oracle_tree_dists()


#: True only inside the monorepo. An sdist ships this file but not its sibling oracles, so the
#: tree-wide checks below have nothing to look at there. The GitHub satellite checkout also has
#: ``oracles/oracles/`` (so ``ORACLES.is_dir()`` alone is true) but not ``gaia/`` next to the
#: repo — without that sibling the dependency sweep cannot see outside consumers.
IN_MONOREPO = ORACLES.is_dir() and (REPO / "gaia").is_dir()

#: Applied per test, NOT as a module-level pytestmark. Some checks here read only files that
#: ship inside the sdist — the version-agreement one below — and those are exactly the checks a
#: consumer should be able to run, so a blanket module skip would silence the most useful test
#: in the file.
monorepo_only = pytest.mark.skipif(
    not IN_MONOREPO,
    reason=f"no sibling oracle tree at {ORACLES} — this check is monorepo-only",
)


@monorepo_only
def test_the_oracle_tree_was_found():
    """A bad glob is how gaia slipped through the first sweep of this same fix."""
    assert len(ORACLE_PYPROJECTS) >= 17, f"only found {len(ORACLE_PYPROJECTS)} oracles under {ORACLES}"


@pytest.mark.parametrize("pyproject", ORACLE_PYPROJECTS, ids=lambda p: p.parent.name)
@monorepo_only
def test_every_oracle_publishes_inside_our_namespace(pyproject):
    """The other half of the rule, which this guard did not check for two days.

    It verified that oracles ask for core by the right name, and that core itself is
    namespaced — but never that the oracles' OWN dist names are. All 17 published as
    `<name>-oracle`, unprefixed, and every one of those names was unclaimed on PyPI. Free is
    not safe: it is claimable. Whoever registers `chronos-oracle` makes `pip install
    chronos-oracle` deliver their code into the `chronos` import namespace this repo uses,
    which is the same attack that already landed here once from the other direction.

    Renamed 2026-07-30, before any of them was published, so this assertion costs nothing
    now and could not have been satisfied later.
    """
    name = _dist_name(pyproject)
    assert name.startswith("aimarket-"), (
        f"{pyproject.parent.name} publishes as {name!r}. Short unprefixed names on PyPI are "
        f"claimable by strangers whose package can install a colliding top-level module; keep "
        f"every distribution inside the namespace this project owns."
    )


@pytest.mark.parametrize("pyproject", ORACLE_TREE_DISTS, ids=lambda p: str(p.parent.name))
@monorepo_only
def test_every_dist_in_the_oracle_tree_is_namespaced(pyproject):
    """The same rule, selected by walking the tree instead of by pattern.

    This is the selector that finally sees `aimarket-platon` — which passes the `aimarket-`
    check, but is the only member of the family not following the `aimarket-oracle-*` shape its
    18 siblings use. That inconsistency is noted rather than asserted: platon 0.1.0 is already
    published, so its name can be changed only by starting a new one.
    """
    name = _dist_name(pyproject)
    assert name.startswith("aimarket-"), (
        f"{pyproject} publishes as {name!r}. Short unprefixed names on PyPI are claimable by "
        f"strangers whose package can install a colliding top-level module."
    )


@monorepo_only
def test_the_tree_walk_sees_what_the_glob_and_the_dependency_sweep_missed():
    """Three selectors have each missed platon once; this pins that it is now covered."""
    walked = {p.parent.name for p in ORACLE_TREE_DISTS}
    assert "backend" in walked or any("platon" in str(p) for p in ORACLE_TREE_DISTS), (
        f"the tree walk still does not reach platon; found {sorted(walked)}"
    )
    assert len(ORACLE_TREE_DISTS) > len(ORACLE_PYPROJECTS), (
        f"the walk found {len(ORACLE_TREE_DISTS)} dists but the glob found "
        f"{len(ORACLE_PYPROJECTS)} — the walk must be a strict superset"
    )


@monorepo_only
def test_the_dependency_sweep_reaches_outside_the_oracle_tree():
    """gaia/ depends on core and lives outside oracles/, which is how it was missed twice."""
    assert len(CORE_CONSUMERS) >= len(ORACLE_PYPROJECTS), (
        f"the dependency sweep found {len(CORE_CONSUMERS)} core consumers but the glob found "
        f"{len(ORACLE_PYPROJECTS)} oracles — the sweep must be a superset"
    )
    names = {p.parent.name for p in CORE_CONSUMERS}
    assert "gaia" in names, f"gaia is a core consumer and must be in scope; found {sorted(names)}"


@pytest.mark.parametrize("pyproject", CORE_CONSUMERS, ids=lambda p: str(p.parent.name))
@monorepo_only
def test_every_core_consumer_is_namespaced(pyproject):
    """Same rule as above, applied by dependency edge rather than by directory."""
    name = _dist_name(pyproject)
    assert name.startswith("aimarket-"), (
        f"{pyproject} publishes as {name!r} while depending on {CORE_DIST!r}. A package built "
        f"against core follows core's packaging rules wherever it lives."
    )


def test_the_package_and_pyproject_agree_on_the_version():
    """A released package that misreports its own version is unfixable after upload.

    Not hypothetical in this repo: aimarket-agent shipped 2.1.2 with __version__ = "2.1.1",
    which is why it grew this exact test. Core had no __version__ at all, so nothing could say
    which core an oracle was running against — and the 0.2.0-vs-0.3.0 distinction is precisely
    what decides whether the free-tier fields exist.

    Deliberately NOT skipped outside the monorepo: this reads only files that ship in the sdist,
    so it is a check a consumer can run, and it is the one most worth running.
    """
    from oracle_core import __version__

    m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', CORE_PYPROJECT.read_text(), re.M)
    assert m, f"{CORE_PYPROJECT} declares no version"
    assert __version__ == m.group(1), (
        f"oracle_core.__version__ is {__version__!r} but pyproject says {m.group(1)!r}"
    )


def test_core_is_namespaced():
    assert CORE_DIST.startswith("aimarket-"), (
        f"core publishes as {CORE_DIST!r}. Unprefixed names collide with strangers on PyPI; "
        "keep the shared library inside the namespace this project owns."
    )


@pytest.mark.parametrize("pyproject", ORACLE_PYPROJECTS, ids=lambda p: p.parent.name)
@monorepo_only
def test_oracle_depends_on_core_by_the_name_core_publishes(pyproject):
    deps = _dependency_names(pyproject)
    if CORE_DIST in deps:
        return
    stale = [d for d in deps if "oracle-core" in d or d == "oracle_core"]
    pytest.fail(
        f"{pyproject.parent.name} depends on {stale or deps} but core publishes as "
        f"{CORE_DIST!r}. An unresolvable-locally name is fetched from PyPI, where "
        f"`oracle-core` is somebody else's project."
    )


# A flat-layout project installs its modules at the TOP level of site-packages, which is the
# second half of the incident in this module's docstring: the stranger's `oracle-core` was
# harmful because it installed a colliding top-level `oracle_core`. Renaming a distribution
# fixes who answers `pip install <name>`; it does nothing about what the wheel then unpacks.
#
# Measured across the 23 core consumers on 2026-08-27: exactly two shipped loose modules —
# themis (ten, including `agent`, `models`, `federation`, `auditor`) and basanos (three,
# including `agent`). Both are clone-and-run services whose README installs with `uv sync` and
# whose Dockerfile starts them with `python agent.py`, so neither wants to be importable at
# all; both are now marked `Private :: Do Not Upload`, which PyPI enforces. The other 21 ship a
# package directory and nothing loose.
#
# So the rule is not "never ship a loose module" — it is "a distribution that would put a bare
# module in site-packages must not be publishable". Either give it a package directory, or say
# out loud that it is not for PyPI.
_PRIVATE_CLASSIFIER = "Private :: Do Not Upload"

_WHEEL_TARGET = re.compile(
    r"\[tool\.hatch\.build\.targets\.wheel\](.*?)(?=\n\[|\Z)", re.S
)
_CLASSIFIERS = re.compile(r"^classifiers\s*=\s*\[(.*?)\]", re.M | re.S)
# Entries like `"agent.py",` inside packages/only-include.
_LOOSE_MODULE = re.compile(r'["\']([A-Za-z0-9._-]+\.py)["\']')

# Modules generic enough that two unrelated projects will collide on them. Not exhaustive and
# not meant to be: it exists so the failure message can say *why* a name is dangerous.
_GENERIC = frozenset(
    {"agent", "models", "main", "app", "config", "settings", "utils", "client", "server",
     "federation", "auditor", "schemas", "types", "api", "db", "auth"}
)


def _loose_wheel_modules(pyproject: pathlib.Path) -> list[str]:
    text = pyproject.read_text()
    m = _WHEEL_TARGET.search(text)
    if not m:
        return []
    return sorted(set(_LOOSE_MODULE.findall(m.group(1))))


def _is_private(pyproject: pathlib.Path) -> bool:
    m = _CLASSIFIERS.search(pyproject.read_text())
    return bool(m) and _PRIVATE_CLASSIFIER in m.group(1)


@pytest.mark.parametrize("pyproject", CORE_CONSUMERS, ids=lambda p: str(p.parent.name))
@monorepo_only
def test_a_flat_layout_consumer_is_not_publishable(pyproject):
    loose = _loose_wheel_modules(pyproject)
    if not loose or _is_private(pyproject):
        return
    names = [m[:-3] for m in loose]
    generic = sorted(set(names) & _GENERIC)
    detail = f" — {', '.join(generic)} will collide with other projects" if generic else ""
    pytest.fail(
        f"{pyproject} builds a wheel that installs {len(loose)} bare top-level module(s) "
        f"({', '.join(names)}){detail}, and is not marked {_PRIVATE_CLASSIFIER!r}. Either move "
        f"them into a package directory (`packages = [\"<name>\"]`, as momus, logos, treasury "
        f"and gaia do) or declare the project unpublishable."
    )
