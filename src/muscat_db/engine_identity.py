"""Small metadata probes executed by each engine's own Python interpreter."""


def version_probe(distributions: tuple[str, ...]) -> str:
    """Include the actual installed git commit in logs and product versions.

    A release version alone cannot distinguish two engine commits. Read pip's
    installation metadata without importing the engine. Editable and ordinary
    index installs retain their existing version format; they have no immutable
    installed git revision to report.
    """
    return (
        "import json\n"
        "from importlib.metadata import distribution, PackageNotFoundError\n"
        f"for name in {distributions!r}:\n"
        "    try:\n"
        "        d = distribution(name)\n"
        "    except PackageNotFoundError:\n"
        "        continue\n"
        "    direct = json.loads(d.read_text('direct_url.json') or '{}')\n"
        "    vcs = direct.get('vcs_info', {})\n"
        "    commit = vcs.get('commit_id') if vcs.get('vcs') == 'git' else None\n"
        "    print(d.version + ('+git.' + commit if commit else ''))\n"
        "    break\n"
    )
