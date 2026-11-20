# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""The checks publish.yml and ci.yml make before anything ships (PAF-012).

    python3 tools/check_release.py tag --event EVENT [--ref REF] [--tag TAG] [--sha SHA]
    python3 tools/check_release.py pyz PAF_PYZ TAG
    python3 tools/check_release.py sdist SDIST

tag    What is being published is exactly "v" + phone_a_friend.__version__.
       A published release (EVENT "release") names its tag in TAG, and the
       commit it tags (SHA) must be on main (origin/main). A manual run (EVENT
       "workflow_dispatch") must be on main (REF "refs/heads/main") and
       publishes "v" + __version__. No other event publishes anything. Prints
       the tag, and writes tag= and version= to $GITHUB_OUTPUT when it is set.
pyz    "python3 PAF_PYZ --version" runs and names the version TAG names.
sdist  SDIST, unpacked, holds every file of the checkout's package, tests,
       tools and licences; loads as many tests as the checkout does, none of
       them failing to import; and its suite passes, run from inside it.

A check that fails prints one line saying why (as a GitHub error annotation
under Actions) and exits 1, so the run stops before anything is uploaded.
__version__ is read from the file, without importing or installing the
package. Standard library only, Python 3.9 or newer.
"""

import argparse
import ast
import os
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT = "phone_a_friend/__init__.py"
MAIN = "origin/main"

# What an sdist must carry from the checkout: every file under these
# directories (bytecode aside), and these files, wherever the checkout has
# them. MANIFEST.in is what puts tests/, tools/ and the rest in.
SDIST_DIRS = ("phone_a_friend", "tests", "tools", "LICENSES")
SDIST_FILES = ("CHANGELOG.md", "LICENSE", "README.md", "REUSE.toml", "pyproject.toml")

VERSION = re.compile(r"[0-9][0-9A-Za-z.+!-]*")
VERSION_LINE = re.compile(r"phone_a_friend \(paf\) (\S+)$")

# Run in a subprocess inside a tree: how many tests unittest discovery loads
# from its tests/, and which of them are modules that failed to import.
COUNT_TESTS = """\
import unittest

def walk(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from walk(test)
        else:
            yield test

tests = list(walk(unittest.defaultTestLoader.discover("tests")))
print("TESTS", len(tests))
for test in tests:
    if type(test).__name__ == "_FailedTest":
        print("BROKEN", test.id())
"""


class Refused(Exception):
    """A check failed. The message is the one line the run shows; detail,
    if any, is printed before it (the output of a suite that failed)."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


def last_line(text):
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def read_version(root=None):
    """__version__ from phone_a_friend/__init__.py, read with ast."""
    path = os.path.join(root or ROOT, INIT)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                raise Refused(f"__version__ in {INIT} is not a string")
            if not VERSION.fullmatch(value.value):
                raise Refused(f"__version__ in {INIT} is not a version: {value.value!r}")
            return value.value
    raise Refused(f"no __version__ in {INIT}")


def on_main(sha, main=MAIN, cwd=None):
    """True if commit SHA is on MAIN: MAIN itself, or one of its ancestors."""
    try:
        out = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, main],
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise Refused(f"refusing to publish: cannot tell whether {sha} is on main: {exc}")
    if out.returncode in (0, 1):
        return out.returncode == 0
    why = last_line(out.stderr) or f"git exited {out.returncode}"
    raise Refused(f"refusing to publish: cannot tell whether {sha} is on main: {why}")


def release_tag(event, ref, tag, version, is_on_main):
    """The tag to publish under, or Refused.

    is_on_main is called, with no arguments, only for a release whose tag is
    right, and says whether the commit it tags is on main.
    """
    want = "v" + version
    if event == "release":
        if tag != want:
            raise Refused(
                f"refusing to publish: the release is tagged {tag!r}, but "
                f"phone_a_friend.__version__ is {version!r}, so its tag must be {want!r}"
            )
        if not is_on_main():
            raise Refused(f"refusing to publish: {tag} tags a commit that is not on main")
        return want
    if event == "workflow_dispatch":
        if ref != "refs/heads/main":
            raise Refused(f"refusing to publish: a manual run must be on main, not {ref!r}")
        return want
    raise Refused(f"refusing to publish: {event!r} is not a published release or a manual run")


def check_pyz(pyz, tag, python=None):
    """The version `python3 PYZ --version` names, if it is the one TAG names."""
    if not tag.startswith("v"):
        raise Refused(f"refusing to upload: {tag!r} is not a tag of the form vX.Y.Z")
    env = dict(os.environ, PAF_FIPS_WARNING="0")
    try:
        out = subprocess.run(
            [python or sys.executable, "-I", pyz, "--version"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"refusing to upload: cannot run {pyz}: {exc}")
    if out.returncode != 0:
        why = last_line(out.stderr) or "no message"
        raise Refused(
            f"refusing to upload: python3 {pyz} --version exited {out.returncode}: {why}"
        )
    first = out.stdout.splitlines()[0] if out.stdout.strip() else ""
    match = VERSION_LINE.match(first)
    said = match.group(1) if match else first
    if said != tag[1:]:
        raise Refused(f"refusing to upload: {pyz} says it is {said!r}, but the tag is {tag}")
    return said


def unpack(sdist, dest):
    """Unpack the sdist into dest and return its one top-level directory."""
    try:
        with tarfile.open(sdist) as tf:
            members = tf.getmembers()
            for member in members:
                parts = member.name.split("/")
                if (
                    member.name.startswith("/")
                    or ".." in parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise Refused(f"{sdist}: refusing to unpack {member.name!r}")
            tops = {member.name.split("/")[0] for member in members}
            if len(tops) != 1:
                raise Refused(f"{sdist} holds {len(tops)} top-level entries, not one directory")
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tf.extractall(dest, members=members, **extra)
    except (OSError, tarfile.TarError) as exc:
        raise Refused(f"cannot unpack {sdist}: {exc}")
    top = os.path.join(dest, tops.pop())
    if not os.path.isdir(top):
        raise Refused(f"{sdist} holds a file at its top level, not a directory")
    return top


def shipped_files(root):
    """The files under root that an sdist must carry, as relative paths."""
    found = set()
    for top in SDIST_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, top)):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for name in filenames:
                if not name.endswith((".pyc", ".pyo")):
                    path = os.path.relpath(os.path.join(dirpath, name), root)
                    found.add(path.replace(os.sep, "/"))
    for name in SDIST_FILES:
        if os.path.isfile(os.path.join(root, name)):
            found.add(name)
    return found


def load_tests(tree, python):
    """(how many tests tree/tests loads, the ids of modules that do not import)."""
    out = subprocess.run(
        [python, "-c", COUNT_TESTS], cwd=tree, capture_output=True, text=True
    )
    if out.returncode != 0:
        raise Refused(f"cannot load the tests in {tree}: {last_line(out.stderr)}")
    count, broken = None, []
    for line in out.stdout.splitlines():
        if line.startswith("TESTS "):
            count = int(line.split()[1])
        elif line.startswith("BROKEN "):
            broken.append(line.split(None, 1)[1])
    if count is None:
        raise Refused(f"cannot load the tests in {tree}: no count")
    return count, broken


def check_sdist(sdist, root=None, python=None):
    """(tests run, the suite's closing lines) for an sdist that passes, else Refused."""
    root = root or ROOT
    python = python or sys.executable
    name = os.path.basename(sdist)
    with tempfile.TemporaryDirectory(prefix="paf-sdist-") as tmp:
        tree = unpack(sdist, tmp)
        missing = sorted(shipped_files(root) - shipped_files(tree))
        if missing:
            more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
            raise Refused(
                f"{name} leaves out {len(missing)} file(s) the checkout has: "
                f"{', '.join(missing[:5])}{more}"
            )
        want, _ = load_tests(root, python)
        got, broken = load_tests(tree, python)
        if broken:
            raise Refused(
                f"{name}: {len(broken)} test module(s) do not import from inside it: "
                f"{', '.join(broken[:3])}"
            )
        if got != want:
            raise Refused(f"{name} loads {got} tests, but the checkout loads {want}")
        run = subprocess.run(
            [python, "-m", "unittest", "discover", "-s", "tests"],
            cwd=tree,
            capture_output=True,
            text=True,
        )
        if run.returncode != 0:
            raise Refused(
                f"{name}: its suite fails, run from inside it: {last_line(run.stderr)}",
                detail=run.stdout + run.stderr,
            )
        closing = [line for line in run.stderr.splitlines() if line.strip()][-2:]
        return got, closing


def set_output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def refuse(exc):
    if exc.detail:
        sys.stderr.write(exc.detail.rstrip("\n") + "\n")
    message = str(exc)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{escaped}", flush=True)
    else:
        print(f"check_release: {message}", file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="check_release.py",
        description="The checks the release workflow makes before it ships anything.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("tag", help="the tag being published is v + __version__, on main")
    tag.add_argument("--event", required=True, help="github.event_name")
    tag.add_argument("--ref", default="", help="github.ref")
    tag.add_argument("--tag", default="", help="github.event.release.tag_name")
    tag.add_argument("--sha", default="", help="the commit being published")
    pyz = sub.add_parser("pyz", help="paf.pyz --version names the tag's version")
    pyz.add_argument("pyz")
    pyz.add_argument("tag")
    sdist = sub.add_parser("sdist", help="the sdist is complete and its suite passes")
    sdist.add_argument("sdist")
    args = parser.parse_args(argv)

    try:
        if args.command == "tag":
            version = read_version()

            def is_on_main():
                if not args.sha:
                    raise Refused("refusing to publish: no commit (--sha) to check is on main")
                return on_main(args.sha)

            published = release_tag(args.event, args.ref, args.tag, version, is_on_main)
            set_output(tag=published, version=version)
            print(f"publishing {published}")
        elif args.command == "pyz":
            said = check_pyz(args.pyz, args.tag)
            print(f"{args.pyz} is {said}, as {args.tag} says")
        else:
            count, closing = check_sdist(args.sdist)
            print(f"{os.path.basename(args.sdist)}: complete; {count} tests load; " + " ".join(closing))
    except Refused as exc:
        return refuse(exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
