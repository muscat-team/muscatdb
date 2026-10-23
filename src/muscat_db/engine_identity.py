"""Small metadata probes executed by each engine's own Python interpreter."""

# Local-version label marking an editable (dev checkout) install, whose
# reported commit can change while muscat-db is running.
EDITABLE_LABEL = ".editable"


def version_probe(distributions: tuple[str, ...]) -> str:
    """Include the actual installed git commit in logs and product versions.

    A release version alone cannot distinguish two engine commits. Read pip's
    installation metadata without importing the engine:

    * pinned git install: ``<version>+git.<commit>`` (immutable)
    * editable install from a git checkout: ``<version>+git.<HEAD>.editable``,
      plus ``.dirty`` when tracked files differ from HEAD. This is the
      checkout's state at probe time, not an immutable revision, and the
      release version is whatever was current when it was installed.
    * anything else: ``<version>`` unchanged

    The editable source directory recorded at install time can go stale (the
    checkout was installed through a mount path that no longer exists), so
    fall back to where the import system resolves the top-level module.
    ``find_spec`` locates a top-level module without executing it.
    """
    return (
        "import json, os, subprocess\n"
        "from importlib.metadata import distribution, PackageNotFoundError\n"
        "from importlib.util import find_spec\n"
        "from urllib.parse import unquote, urlparse\n"
        "def source_dir(d, url):\n"
        "    src = unquote(urlparse(url).path)\n"
        "    if os.path.isdir(src):\n"
        "        return src\n"
        "    tops = (d.read_text('top_level.txt') or '').split()\n"
        "    for top in tops or [d.metadata['Name'].replace('-', '_')]:\n"
        "        try:\n"
        "            spec = find_spec(top)\n"
        "        except Exception:\n"
        "            spec = None\n"
        "        if spec is not None and spec.origin:\n"
        "            return os.path.dirname(spec.origin)\n"
        "    return src\n"
        "def checkout(src):\n"
        "    try:\n"
        "        git = lambda *a: subprocess.run(['git', '-C', src, *a], check=True,\n"
        "            capture_output=True, text=True, timeout=2).stdout.strip()\n"
        "        head = git('rev-parse', 'HEAD')\n"
        "        dirty = git('status', '--porcelain', '--untracked-files=no')\n"
        "    except Exception:\n"
        "        return ''\n"
        f"    return '+git.' + head + {EDITABLE_LABEL!r} + ('.dirty' if dirty else '')\n"
        f"for name in {distributions!r}:\n"
        "    try:\n"
        "        d = distribution(name)\n"
        "    except PackageNotFoundError:\n"
        "        continue\n"
        "    direct = json.loads(d.read_text('direct_url.json') or '{}')\n"
        "    vcs = direct.get('vcs_info', {})\n"
        "    commit = vcs.get('commit_id') if vcs.get('vcs') == 'git' else None\n"
        "    suffix = '+git.' + commit if commit else ''\n"
        "    if not suffix and direct.get('dir_info', {}).get('editable'):\n"
        "        suffix = checkout(source_dir(d, direct.get('url', '')))\n"
        "    print(d.version + suffix)\n"
        "    break\n"
    )


def is_mutable_version(version: str) -> bool:
    """True when ``version`` came from an editable checkout and may change."""
    return EDITABLE_LABEL in version
