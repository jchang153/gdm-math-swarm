"""Researcher-only material kept outside the public repository.

The analysis rubrics, judge and incident prompts, seeded artifacts, exploit fixtures, captured
agent instructions and the problems/VM records live host-only in the git-ignored ``private/``
directory at the repository root, under their original repository paths (for example
``private/research/analysis/judge_rubric.md``). Code reads them through this module; a
checkout without ``private/`` still imports, and anything that needs the material fails with
``PrivateMaterialMissing`` naming the missing file. Tests that need it are skipped.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRIVATE = REPO / "private"


class PrivateMaterialMissing(ImportError):
    """Researcher-only material is not present in this checkout (git-ignored ``private/``)."""


def path(relative: str) -> Path:
    """The host-only copy of a repository-relative researcher-only file."""
    return PRIVATE / relative


def available(*relatives: str) -> bool:
    return all(path(r).exists() for r in relatives)


def missing_message(relative: str) -> str:
    return (f"researcher-only material not present: private/{relative} (git-ignored; copy the "
            "private files into private/ to use it)")


def skip_unless_available(*relatives: str) -> None:
    """Skip the calling test module when researcher-only material is missing."""
    for relative in relatives:
        if not path(relative).exists():
            raise unittest.SkipTest(missing_message(relative))


def skip_decorator(*relatives: str):
    """``unittest.skipUnless`` for researcher-only material."""
    missing = [r for r in relatives if not path(r).exists()]
    return unittest.skipUnless(not missing, missing_message(missing[0]) if missing else "")


def install_module(fullname: str, relative: str) -> types.ModuleType:
    """Import ``private/<relative>`` as module ``fullname`` (a submodule of an existing package).

    Without the file, a placeholder module is installed whose attributes raise
    ``PrivateMaterialMissing``, so that importing the package still works."""
    if fullname in sys.modules:
        return sys.modules[fullname]
    parent_name, _, child = fullname.rpartition(".")
    source = path(relative)
    if source.exists():
        spec = importlib.util.spec_from_file_location(fullname, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[fullname] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(fullname, None)
            raise
    else:
        module = types.ModuleType(fullname, f"Placeholder: {missing_message(relative)}")
        module.__private_material_missing__ = relative

        def __getattr__(name, _relative=relative):
            if name.startswith("__") and name.endswith("__"):
                raise AttributeError(name)
            raise PrivateMaterialMissing(missing_message(_relative))

        module.__getattr__ = __getattr__
        sys.modules[fullname] = module
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child, module)
    return module
