"""Standalone adapter run by prose's Python, before importing the engine.

The pinned prose2 ConfigManager has no configuration-directory argument. This
import adapter supplies its frozen configuration before prose.__init__ creates
CONFIG; it never changes the engine checkout, HOME, or the shared ~/.prose tree.
Keep this module compatible with prose's Python 3.10 and standard-library-only
until the snapshot is validated and the installed engine identity is checked.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
from pathlib import Path
import runpy
import sys

SNAPSHOT = Path(__file__).with_name("_prose_telescopes.yaml")
SNAPSHOT_SHA256 = "39503fce597e2d5fd349fa8009ed5068222fc6e31f098cf50e62f2a17f314a08"
MODULES = {"prose.scripts.run_photometry", "prose.scripts.postprocess_lightcurves"}


def snapshot_bytes() -> bytes:
    data = SNAPSHOT.read_bytes()
    if hashlib.sha256(data).hexdigest() != SNAPSHOT_SHA256:
        raise RuntimeError("frozen prose telescope snapshot checksum mismatch")
    return data


def install_config(snapshot: dict) -> None:
    if "prose" in sys.modules:
        raise RuntimeError("frozen configuration must be installed before importing prose")

    class ConfigFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "prose.config":
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or spec.loader is None:
                raise RuntimeError("pinned prose.config is unavailable")
            original_loader = spec.loader

            class ConfigLoader(importlib.abc.Loader):
                def create_module(self, spec):
                    return original_loader.create_module(spec)

                def exec_module(self, module):
                    original_loader.exec_module(module)

                    class FrozenConfig(module.ConfigManager):
                        def __init__(self):
                            self.folder_path = SNAPSHOT.parent
                            self.config_file = SNAPSHOT
                            self.config = copy.deepcopy(snapshot["settings"])
                            self.telescopes_dict = self.build_telescopes_dict()
                            self.logs = []

                        def build_telescopes_dict(self):
                            return copy.deepcopy(snapshot["telescopes"])

                        def check_config_file(self, load=False):
                            if load:
                                self.config = copy.deepcopy(snapshot["settings"])

                        def check_builtins_changes(self):
                            # The effective builtin definitions are in the snapshot.
                            pass

                        def save(self):
                            raise RuntimeError("production prose configuration is frozen")

                        def save_telescope_file(self, file):
                            raise RuntimeError("production prose configuration is frozen")

                    module.ConfigManager = FrozenConfig

            spec.loader = ConfigLoader()
            return spec

    sys.meta_path.insert(0, ConfigFinder())


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    checking = args == ["--check"]
    if not checking and (not args or args[0] not in MODULES):
        raise ValueError("expected a supported prose module followed by its arguments")
    data = snapshot_bytes()
    import yaml
    snapshot = yaml.full_load(data)
    if snapshot.get("schema") != 1:
        raise RuntimeError("unsupported prose snapshot schema")
    direct = json.loads(importlib.metadata.distribution("prose").read_text("direct_url.json") or "{}")
    if (direct.get("url", "").removesuffix(".git") != snapshot["repository"]
            or direct.get("vcs_info", {}).get("commit_id") != snapshot["engine_commit"]
            or direct.get("dir_info", {}).get("editable")):
        raise RuntimeError("frozen configuration requires its matching pinned prose installation")
    # The pinned Python 3.10/Linux pipeline uses fork. A spawn/forkserver child
    # could import prose afresh without this adapter; refuse that configuration.
    import multiprocess
    if multiprocess.get_start_method() != "fork":
        raise RuntimeError("frozen prose configuration currently requires fork workers")
    install_config(snapshot)
    if checking:
        import prose
        print(json.dumps({"prose_config_sha256": SNAPSHOT_SHA256,
                          "telescope_aliases": len(prose.CONFIG.telescopes_dict)}))
        return 0
    print(f"MuSCAT prose telescope snapshot sha256: {SNAPSHOT_SHA256}", flush=True)
    sys.argv = args
    runpy.run_module(args[0], run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
