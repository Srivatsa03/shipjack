"""Every data file the code reads must ship in the installed package, not only in the source tree.

A non-editable install (the Docker image) once lacked the sample Shopify export, so the deployed store
crashed at start while every local test, run from the source tree, passed.
"""

import fnmatch
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_every_data_file_is_declared_as_package_data():
    globs = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["shipjack"]
    files = [p.relative_to(ROOT / "shipjack").as_posix() for p in (ROOT / "shipjack" / "data").iterdir() if p.is_file()]
    missing = [f for f in files if not any(fnmatch.fnmatch(f, g) for g in globs)]
    assert files and not missing, f"not packaged: {missing}"
