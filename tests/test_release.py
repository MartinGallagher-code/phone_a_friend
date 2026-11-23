# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""The release checks (tools/check_release.py) and the workflows that run them."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_pyz  # noqa: E402
import check_release  # noqa: E402
from check_release import Refused  # noqa: E402
import phone_a_friend  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - only CI's test job installs PyYAML
    yaml = None

WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def run_main(*argv, env=None):
    """(exit status, stdout, stderr) of check_release.main(argv)."""
    out, err = io.StringIO(), io.StringIO()
    base = {"GITHUB_ACTIONS": "", "GITHUB_OUTPUT": ""}
    with mock.patch.dict(os.environ, dict(base, **(env or {}))), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = check_release.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TagTest(unittest.TestCase):
    """The tag being published must be exactly v + __version__, on main."""

    def tag(self, event, ref="", tag="", version="2.1.2", on_main=True):
        return check_release.release_tag(event, ref, tag, version, lambda: on_main)

    def assertRefused(self, *args, **kwargs):
        with self.assertRaises(Refused) as cm:
            self.tag(*args, **kwargs)
        message = str(cm.exception)
        self.assertTrue(message.startswith("refusing to publish: "), message)
        self.assertEqual(len(message.splitlines()), 1, message)
        return message

    def test_release_tagged_with_the_version(self):
        self.assertEqual(self.tag("release", "refs/tags/v2.1.2", "v2.1.2"), "v2.1.2")

    def test_release_with_another_tag(self):
        for bad in ("v2.1.3", "2.1.2", "v2.1.2rc1", "V2.1.2", "v2.1.2 ", "v2.1", ""):
            with self.subTest(tag=bad):
                message = self.assertRefused("release", "refs/tags/" + bad, bad)
                self.assertIn(repr(bad), message)
                self.assertIn("'v2.1.2'", message)

    def test_release_off_main(self):
        message = self.assertRefused("release", "refs/tags/v2.1.2", "v2.1.2", on_main=False)
        self.assertIn("not on main", message)

    def test_wrong_tag_is_refused_before_asking_git(self):
        def ask():
            raise AssertionError("asked whether the commit is on main")

        with self.assertRaises(Refused):
            check_release.release_tag("release", "", "v9.9.9", "2.1.2", ask)

    def test_manual_run_on_main(self):
        self.assertEqual(self.tag("workflow_dispatch", "refs/heads/main"), "v2.1.2")

    def test_manual_run_off_main(self):
        for ref in ("refs/heads/claude/x", "refs/tags/v2.1.2", "refs/heads/mainline", ""):
            with self.subTest(ref=ref):
                self.assertIn(repr(ref), self.assertRefused("workflow_dispatch", ref))

    def test_other_events(self):
        # Not even a pushed tag that matches, or a push to main.
        for event in ("push", "pull_request", "schedule", "workflow_call", ""):
            for ref in ("refs/tags/v2.1.2", "refs/heads/main"):
                with self.subTest(event=event, ref=ref):
                    message = self.assertRefused(event, ref, "v2.1.2")
                    self.assertIn("is not a published release or a manual run", message)


class VersionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="paf-version-")
        self.addCleanup(shutil.rmtree, self.root)
        os.mkdir(os.path.join(self.root, "phone_a_friend"))

    def write(self, text):
        with open(os.path.join(self.root, check_release.INIT), "w") as fh:
            fh.write(textwrap.dedent(text))

    def test_reads_the_package_version_without_importing_it(self):
        self.assertEqual(check_release.read_version(), phone_a_friend.__version__)
        self.write('"""doc"""\nimport os\nx = 1\n__version__ = "3.0.0rc1"\n')
        self.assertEqual(check_release.read_version(self.root), "3.0.0rc1")

    def test_no_version(self):
        self.write('VERSION = "1.0"\n')
        with self.assertRaisesRegex(Refused, "no __version__"):
            check_release.read_version(self.root)

    def test_version_not_a_string(self):
        self.write("__version__ = (2, 1, 2)\n")
        with self.assertRaisesRegex(Refused, "not a string"):
            check_release.read_version(self.root)

    def test_version_that_is_not_one(self):
        for bad in ("2.1.2\\nx=1", "", "v2.1.2", "2.1.2 extra", "2.1.2;x"):
            with self.subTest(version=bad):
                self.write(f'__version__ = "{bad}"\n')
                with self.assertRaisesRegex(Refused, "not a version"):
                    check_release.read_version(self.root)


@unittest.skipUnless(shutil.which("git"), "needs git")
class OnMainTest(unittest.TestCase):
    def git(self, *argv):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", *argv],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, message):
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="paf-git-")
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.first = self.commit("one")
        self.git("checkout", "-q", "-b", "side")
        self.side = self.commit("side")
        self.git("checkout", "-q", "main")
        self.head = self.commit("two")

    def test_on_main(self):
        self.assertTrue(check_release.on_main(self.head, "main", self.repo))
        self.assertTrue(check_release.on_main(self.first, "main", self.repo))
        self.assertFalse(check_release.on_main(self.side, "main", self.repo))

    def test_cannot_tell(self):
        for sha, main in (("0" * 40, "main"), (self.head, "origin/main")):
            with self.subTest(sha=sha, main=main):
                with self.assertRaisesRegex(Refused, "cannot tell whether"):
                    check_release.on_main(sha, main, self.repo)

    def test_without_git(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")), \
                self.assertRaisesRegex(Refused, "cannot tell whether"):
            check_release.on_main(self.head, "main", self.repo)


class TagCommandTest(unittest.TestCase):
    version = phone_a_friend.__version__

    def test_manual_run_writes_the_tag_for_later_jobs(self):
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out:
            code, stdout, stderr = run_main(
                "tag", "--event", "workflow_dispatch", "--ref", "refs/heads/main",
                env={"GITHUB_OUTPUT": out.name},
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(stdout, f"publishing v{self.version}\n")
            self.assertEqual(out.read(), f"tag=v{self.version}\nversion={self.version}\n")

    def test_release_on_main(self):
        with mock.patch.object(check_release, "on_main", return_value=True) as on_main:
            code, stdout, _ = run_main(
                "tag", "--event", "release", "--tag", f"v{self.version}", "--sha", "abc123"
            )
        self.assertEqual(code, 0)
        on_main.assert_called_once_with("abc123")
        self.assertEqual(stdout, f"publishing v{self.version}\n")

    def test_refused_in_one_line(self):
        code, stdout, stderr = run_main("tag", "--event", "release", "--tag", "v0.0.1", "--sha", "x")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(len(stderr.splitlines()), 1)
        self.assertTrue(stderr.startswith("check_release: refusing to publish: "), stderr)

    def test_refused_as_an_error_annotation_under_actions(self):
        code, stdout, stderr = run_main(
            "tag", "--event", "workflow_dispatch", "--ref", "refs/heads/dev%1",
            env={"GITHUB_ACTIONS": "true"},
        )
        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(
            stdout,
            "::error::refusing to publish: a manual run must be on main, "
            "not 'refs/heads/dev%251'\n",
        )

    def test_release_needs_the_commit(self):
        code, _, stderr = run_main("tag", "--event", "release", "--tag", f"v{self.version}")
        self.assertEqual(code, 1)
        self.assertIn("no commit (--sha)", stderr)

    def test_usage(self):
        for argv in ([], ["tag"], ["pyz", "paf.pyz"], ["nonsense"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm:
                run_main(*argv)
            self.assertEqual(cm.exception.code, 2)


class PyzTest(unittest.TestCase):
    """paf.pyz is uploaded only if it says the version the tag names."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paf-release-pyz-")
        cls.pyz = os.path.join(cls.tmp, "paf.pyz")
        build_pyz.build(cls.pyz)
        # The same build from a tree whose version is 9.9.9.
        other = os.path.join(cls.tmp, "tree")
        shutil.copytree(os.path.join(ROOT, "phone_a_friend"),
                        os.path.join(other, "phone_a_friend"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "tools"), os.path.join(other, "tools"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        init = os.path.join(other, "phone_a_friend", "__init__.py")
        with open(init) as fh:
            text = fh.read()
        with open(init, "w") as fh:
            fh.write(text.replace(f'"{phone_a_friend.__version__}"', '"9.9.9"'))
        cls.other = os.path.join(cls.tmp, "other.pyz")
        with mock.patch.object(build_pyz, "ROOT", other):
            build_pyz.build(cls.other)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_tag_s_version(self):
        tag = "v" + phone_a_friend.__version__
        self.assertEqual(check_release.check_pyz(self.pyz, tag), phone_a_friend.__version__)
        self.assertEqual(check_release.check_pyz(self.other, "v9.9.9"), "9.9.9")

    def test_built_from_another_version(self):
        with self.assertRaisesRegex(Refused, r"refusing to upload: .* says it is '9\.9\.9'"):
            check_release.check_pyz(self.other, "v" + phone_a_friend.__version__)

    def test_tag_naming_another_version(self):
        for tag in ("v9.9.9", "v" + phone_a_friend.__version__ + "rc1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(Refused, "but the tag is"):
                check_release.check_pyz(self.pyz, tag)

    def test_tag_without_v(self):
        with self.assertRaisesRegex(Refused, "not a tag of the form"):
            check_release.check_pyz(self.pyz, phone_a_friend.__version__)

    def test_a_pyz_that_does_not_run(self):
        broken = os.path.join(self.tmp, "broken.pyz")
        with open(broken, "w") as fh:
            fh.write("raise SystemExit('paf: broken')\n")
        with self.assertRaisesRegex(Refused, r"--version exited 1: paf: broken"):
            check_release.check_pyz(broken, "v1.0.0")
        with self.assertRaisesRegex(Refused, "cannot run"):
            check_release.check_pyz(self.pyz, "v1.0.0", python=os.path.join(self.tmp, "nope"))

    def test_a_pyz_that_prints_something_else(self):
        odd = os.path.join(self.tmp, "odd.pyz")
        for text, said in (("print('paf 1.0.0')", "paf 1.0.0"), ("pass", "")):
            with self.subTest(said=said):
                with open(odd, "w") as fh:
                    fh.write(text + "\n")
                with self.assertRaisesRegex(Refused, f"says it is {said!r}"):
                    check_release.check_pyz(odd, "v1.0.0")

    def test_command(self):
        tag = "v" + phone_a_friend.__version__
        code, stdout, _ = run_main("pyz", self.pyz, tag)
        self.assertEqual((code, stdout), (0, f"{self.pyz} is {tag[1:]}, as {tag} says\n"))
        code, stdout, stderr = run_main("pyz", self.other, tag)
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("says it is '9.9.9'", stderr)


PASSING = """\
import unittest

class T(unittest.TestCase):
    def test_one(self):
        pass

    def test_two(self):
        pass
"""


class SdistTest(unittest.TestCase):
    """The sdist carries the checkout's files, and its own suite passes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-release-sdist-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.files = {
            "pyproject.toml": "",
            "README.md": "",
            "CHANGELOG.md": "",
            "LICENSE": "",
            "REUSE.toml": "",
            "LICENSES/GPL-3.0-or-later.txt": "",
            "phone_a_friend/__init__.py": "",
            "tests/test_a.py": PASSING,
            "tests/helper.py": "",
            "tools/tool.py": "",
        }
        self.root = os.path.join(self.tmp, "checkout")
        self.write_tree(self.root, self.files)
        # Neither the checkout's bytecode nor its other files are asked for.
        self.write_tree(self.root, {"tests/__pycache__/x.pyc": "", "tools/old.pyc": "",
                                    "docs/index.rst": "", ".coverage": ""})

    def write_tree(self, root, files):
        for name, text in files.items():
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)

    def sdist(self, files, top="pkg-1.0", name="pkg-1.0.tar.gz"):
        tree = os.path.join(self.tmp, "tree-" + name)
        self.write_tree(os.path.join(tree, top), files)
        path = os.path.join(self.tmp, name)
        with tarfile.open(path, "w:gz") as tf:
            tf.add(os.path.join(tree, top), top)
        return path

    def check(self, sdist):
        return check_release.check_sdist(sdist, root=self.root)

    def assertRefused(self, sdist, pattern):
        with self.assertRaisesRegex(Refused, pattern) as cm:
            self.check(sdist)
        return cm.exception

    def test_complete(self):
        count, closing = self.check(self.sdist(self.files))
        self.assertEqual(count, 2)
        self.assertEqual(closing[-1], "OK")
        self.assertTrue(closing[0].startswith("Ran 2 tests"), closing)

    def test_leaves_files_out(self):
        files = dict(self.files)
        for name in ("tests/helper.py", "tools/tool.py", "CHANGELOG.md", "LICENSES/GPL-3.0-or-later.txt"):
            del files[name]
        exc = self.assertRefused(self.sdist(files), "leaves out 4 file")
        self.assertIn("CHANGELOG.md, LICENSES/GPL-3.0-or-later.txt, tests/helper.py, tools/tool.py",
                      str(exc))

    def test_leaves_many_files_out(self):
        more = {f"tools/t{i}.py": "" for i in range(7)}
        self.write_tree(self.root, more)
        self.assertRefused(self.sdist(self.files), r"leaves out 7 file\(s\) .* and 2 more$")

    def test_a_test_module_that_does_not_import(self):
        files = dict(self.files, **{"tests/test_b.py": "import v0_store\n"})
        self.assertRefused(self.sdist(files), r"1 test module\(s\) do not import .*test_b")

    def test_loads_another_number_of_tests(self):
        files = dict(self.files, **{"tests/test_c.py": PASSING})
        self.assertRefused(self.sdist(files), "loads 4 tests, but the checkout loads 2")

    def test_suite_fails(self):
        failing = PASSING.replace("pass\n\n", "self.fail('no')\n\n", 1)
        exc = self.assertRefused(self.sdist(dict(self.files, **{"tests/test_a.py": failing})),
                                 r"its suite fails, run from inside it: FAILED \(failures=1\)")
        self.assertIn("AssertionError: no", exc.detail)

    def test_tests_that_cannot_load(self):
        files = dict(self.files, **{"tests/test_z.py": "import os\nos._exit(3)\n"})
        self.assertRefused(self.sdist(files), "cannot load the tests in")
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="hi\n")), \
                self.assertRaisesRegex(Refused, "no count"):
            check_release.load_tests(self.root, sys.executable)

    def test_unsafe_or_odd_archives(self):
        evil = os.path.join(self.tmp, "evil.tar.gz")
        with tarfile.open(evil, "w:gz") as tf:
            info = tarfile.TarInfo("pkg-1.0/../../escaped.txt")
            tf.addfile(info, io.BytesIO(b""))
        self.assertRefused(evil, "refusing to unpack")
        linked = os.path.join(self.tmp, "link.tar.gz")
        with tarfile.open(linked, "w:gz") as tf:
            info = tarfile.TarInfo("pkg-1.0/link")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tf.addfile(info)
        self.assertRefused(linked, "refusing to unpack")
        two = os.path.join(self.tmp, "two.tar.gz")
        with tarfile.open(two, "w:gz") as tf:
            for name in ("a/x", "b/x"):
                tf.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
        self.assertRefused(two, "holds 2 top-level entries")
        flat = os.path.join(self.tmp, "flat.tar.gz")
        with tarfile.open(flat, "w:gz") as tf:
            tf.addfile(tarfile.TarInfo("README"), io.BytesIO(b""))
        self.assertRefused(flat, "holds a file at its top level")
        garbage = os.path.join(self.tmp, "garbage.tar.gz")
        with open(garbage, "w") as fh:
            fh.write("not a tar\n")
        self.assertRefused(garbage, "cannot unpack")
        self.assertRefused(os.path.join(self.tmp, "missing.tar.gz"), "cannot unpack")

    def test_command(self):
        sdist = self.sdist(self.files)
        with mock.patch.object(check_release, "ROOT", self.root):
            code, stdout, _ = run_main("sdist", sdist)
            self.assertEqual(code, 0)
            self.assertTrue(stdout.startswith("pkg-1.0.tar.gz: complete; 2 tests load; Ran 2 tests"),
                            stdout)
            failing = PASSING.replace("pass\n\n", "self.fail('no')\n\n", 1)
            bad = self.sdist(dict(self.files, **{"tests/test_a.py": failing}), name="bad.tar.gz")
            code, stdout, stderr = run_main("sdist", bad)
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("AssertionError: no", stderr)
        self.assertTrue(stderr.splitlines()[-1].startswith("check_release: bad.tar.gz: its suite fails"))


@unittest.skipUnless(yaml, "needs PyYAML")
@unittest.skipUnless(os.path.isdir(WORKFLOWS), "needs the checkout's .github/workflows")
class WorkflowTest(unittest.TestCase):
    """What publish.yml waits for, checked statically: it cannot run on a pull request."""

    @staticmethod
    def load(name):
        with open(os.path.join(WORKFLOWS, name)) as fh:
            workflow = yaml.safe_load(fh)
        # YAML 1.1 reads the key "on" as True.
        workflow["on"] = workflow.pop(True, workflow.get("on"))
        return workflow

    @staticmethod
    def needs(job):
        needs = job.get("needs", [])
        return {needs} if isinstance(needs, str) else set(needs)

    @staticmethod
    def runs(job):
        return [step.get("run", "") for step in job.get("steps", [])]

    def setUp(self):
        self.publish = self.load("publish.yml")
        self.ci = self.load("ci.yml")

    def test_publish_triggers(self):
        self.assertEqual(set(self.publish["on"]), {"release", "workflow_dispatch"})
        self.assertEqual(self.publish["on"]["release"], {"types": ["published"]})

    def test_one_run_at_a_time_per_tag(self):
        concurrency = self.publish["concurrency"]
        self.assertIn("github.event.release.tag_name", concurrency["group"])
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_everything_waits_for_verify_and_ci(self):
        jobs = self.publish["jobs"]
        self.assertEqual(set(jobs), {"verify", "ci", "publish", "pyz"})
        verify = " ".join(self.runs(jobs["verify"]))
        self.assertIn("tools/check_release.py tag", verify)
        self.assertEqual(jobs["verify"]["steps"][0]["with"]["fetch-depth"], 0)
        self.assertEqual(jobs["ci"]["uses"], "./.github/workflows/ci.yml")
        self.assertEqual(self.needs(jobs["ci"]), {"verify"})
        for name in ("publish", "pyz"):
            with self.subTest(job=name):
                self.assertTrue({"verify", "ci"} <= self.needs(jobs[name]))
                self.assertNotIn("always()", str(jobs[name].get("if", "")))

    def test_publish_uploads_what_ci_built(self):
        publish = self.publish["jobs"]["publish"]
        steps = [step.get("uses", "") for step in publish["steps"]]
        self.assertEqual(steps[0], "actions/download-artifact@v4")
        self.assertEqual(publish["steps"][0]["with"]["name"], "dist")
        self.assertTrue(steps[-1].startswith("pypa/gh-action-pypi-publish@"))
        self.assertEqual(publish["permissions"], {"id-token": "write"})
        build = self.ci["jobs"]["build"]
        uploads = [s["with"]["name"] for s in build["steps"]
                   if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(uploads, ["dist"])

    def test_pyz_is_checked_before_it_is_attached(self):
        pyz = self.publish["jobs"]["pyz"]
        self.assertEqual(pyz["if"], "github.event_name == 'release'")
        runs = self.runs(pyz)
        check = next(i for i, run in enumerate(runs) if "check_release.py pyz" in run)
        upload = next(i for i, run in enumerate(runs) if "gh release upload" in run)
        self.assertLess(check, upload)
        names = [s["with"]["name"] for s in pyz["steps"]
                 if s.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual(names, ["paf.pyz"])

    def test_ci_can_be_called_and_has_every_check(self):
        self.assertIn("workflow_call", self.ci["on"])
        self.assertTrue({"test", "fips", "pyz", "pyz-rocky", "version", "reuse", "build"}
                        <= set(self.ci["jobs"]))
        build = " ".join(self.runs(self.ci["jobs"]["build"]))
        self.assertIn("tools/check_release.py sdist", build)


if __name__ == "__main__":
    unittest.main()
