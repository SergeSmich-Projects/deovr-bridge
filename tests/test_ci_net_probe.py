# -*- coding: utf-8 -*-
"""Тесты задачи scripts/ci/net_probe.py и публикации отчёта.

Главное, что здесь защищается: секреты из заголовков и тел ответов НЕ попадают
в отчёт, а полезные данные (infohash, magnet-ссылка, <title>) сохраняются.
Ради этого задача и существует - она выполняется на компьютере пользователя.
"""

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_DIR = os.path.join(ROOT, "scripts", "ci")
TEMPLATES = os.path.join(CI_DIR, "reports-branch")
if CI_DIR not in sys.path:
    sys.path.insert(0, CI_DIR)

import net_probe  # noqa: E402
import publish_report  # noqa: E402

SECRET_COOKIE = "PHPSESSID=cookievalue123456"
SECRET_BEARER = "supersecrettokenvalue"
SECRET_PASSWORD = "TopSecret123"
SECRET_FORM = "FORMHIDDENVALUE9876"
INFOHASH = "abcdef0123456789abcdef0123456789abcdef01"

PAGE = (
    "<html><head><title>Тестовая страница VR</title></head><body>"
    f"<input type='hidden' name='bb_data' value='{SECRET_FORM}'>"
    f"<a href='x?password={SECRET_PASSWORD}&token=zzztoken123'>вход</a>"
    f"<p>magnet:?xt=urn:btih:{INFOHASH}&dn=Some+VR+Scene</p>"
    "<p>Проверка captcha и antibot</p>"
    "</body></html>"
).encode("utf-8")

API = json.dumps({"hits": [{"title": "VR Test", "infohash": INFOHASH}]}).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # тишина в выводе тестов
        return

    def _serve(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Set-Cookie", SECRET_COOKIE)
        self.send_header("Authorization", "Bearer " + SECRET_BEARER)
        self.send_header("X-Powered-By", "fixture")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._serve(API if self.path.startswith("/api") else PAGE,
                    "application/json; charset=utf-8" if self.path.startswith("/api")
                    else "text/html; charset=utf-8")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._serve(API, "application/json; charset=utf-8")


class NetProbeValidationTests(unittest.TestCase):
    """Проверки, которые должны отказывать ДО первого запроса."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="net-probe-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _targets(self, payload):
        path = os.path.join(self.tmp, "targets.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def test_rejects_host_outside_allowlist(self):
        path = self._targets({
            "allowed_hosts": ["example.com"],
            "targets": [{"name": "x", "url": "https://not-allowed.example.org/"}],
        })
        with self.assertRaises(ValueError) as ctx:
            net_probe._load_targets(path)
        self.assertIn("allowed_hosts", str(ctx.exception))

    def test_rejects_unsupported_method(self):
        path = self._targets({
            "allowed_hosts": ["example.com"],
            "targets": [{"name": "x", "url": "https://example.com/", "method": "DELETE"}],
        })
        with self.assertRaises(ValueError) as ctx:
            net_probe._load_targets(path)
        self.assertIn("GET/POST", str(ctx.exception))

    def test_rejects_duplicate_names(self):
        path = self._targets({
            "allowed_hosts": ["example.com"],
            "targets": [
                {"name": "dup", "url": "https://example.com/a"},
                {"name": "dup", "url": "https://example.com/b"},
            ],
        })
        with self.assertRaises(ValueError):
            net_probe._load_targets(path)

    def test_rejects_empty_allowlist(self):
        path = self._targets({"allowed_hosts": [], "targets": []})
        with self.assertRaises(ValueError):
            net_probe._load_targets(path)


class NetProbeHelpersTests(unittest.TestCase):
    def test_redaction_keeps_useful_data(self):
        body = PAGE
        redacted, hits = net_probe._redact(body)
        text = redacted.decode("utf-8", "replace")
        self.assertNotIn(SECRET_PASSWORD, text)
        self.assertNotIn(SECRET_FORM, text)
        self.assertNotIn("zzztoken123", text)
        self.assertIn(INFOHASH, text)
        self.assertIn("magnet:?xt=urn:btih:", text)
        self.assertGreaterEqual(hits, 3)

    def test_redaction_removes_bearer_token(self):
        redacted, _ = net_probe._redact(b"Authorization: Bearer " + SECRET_BEARER.encode())
        self.assertNotIn(SECRET_BEARER.encode(), redacted)

    def test_extract_title_and_signals(self):
        text = PAGE.decode("utf-8")
        self.assertEqual(net_probe._extract_title(text), "Тестовая страница VR")
        self.assertIn("antibot", net_probe._detect_signals(text))
        self.assertIn("captcha", net_probe._detect_signals(text))

    def test_decode_cp1251(self):
        text = net_probe._decode("Привет".encode("cp1251"), "text/html; charset=windows-1251")
        self.assertEqual(text, "Привет")

    def test_decode_never_raises_on_broken_bytes(self):
        self.assertIsInstance(net_probe._decode(b"\xff\xfe\x00broken", ""), str)

    def test_only_whitelisted_headers_are_kept(self):
        for name in ("set-cookie", "cookie", "authorization"):
            self.assertNotIn(name, net_probe.KEEP_HEADERS)


class NetProbeEndToEndTests(unittest.TestCase):
    """Полный цикл на локальном сервере-заглушке."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="net-probe-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "out")
        self.targets = os.path.join(self.tmp, "targets.json")
        with open(self.targets, "w", encoding="utf-8") as fh:
            json.dump({
                "allowed_hosts": ["127.0.0.1"],
                "targets": [
                    {"name": "page", "url": f"http://127.0.0.1:{self.port}/page"},
                    {"name": "api", "url": f"http://127.0.0.1:{self.port}/api",
                     "method": "POST", "json": {"query": "VR"}},
                    {"name": "dead", "url": f"http://127.0.0.1:{self.port + 1}/x"},
                ],
            }, fh)

    def test_report_has_no_secrets_and_keeps_data(self):
        exit_code = net_probe.main(["--targets", self.targets, "--out-dir", self.out])
        self.assertEqual(exit_code, 0)

        raw = open(os.path.join(self.out, "net_probe.json"), encoding="utf-8").read()
        report = json.loads(raw)
        self.assertFalse(report["ok"])  # цель dead недоступна - это часть проверки
        self.assertNotIn(SECRET_COOKIE.split("=")[1], raw)
        self.assertNotIn(SECRET_BEARER, raw)
        self.assertNotIn("Set-Cookie", raw)

        by_name = {item["name"]: item for item in report["results"]}
        self.assertEqual(by_name["page"]["title"], "Тестовая страница VR")
        self.assertEqual(by_name["api"]["status"], 200)
        self.assertTrue(by_name["dead"]["error"])

        with gzip.open(os.path.join(self.out, by_name["page"]["body_file"]), "rb") as fh:
            body = fh.read().decode("utf-8", "replace")
        self.assertIn(INFOHASH, body)
        self.assertNotIn(SECRET_PASSWORD, body)
        self.assertIn("[REDACTED]", body)

    def test_history_accumulates_and_truncation_is_flagged(self):
        net_probe.main(["--targets", self.targets, "--out-dir", self.out])
        net_probe.main(["--targets", self.targets, "--out-dir", self.out])
        report = json.load(open(os.path.join(self.out, "net_probe.json"), encoding="utf-8"))
        self.assertEqual(len(report["history"]["page"]), 2)

        tiny = os.path.join(self.tmp, "out_tiny")
        net_probe.main(["--targets", self.targets, "--out-dir", tiny,
                        "--max-body", "120", "--only", "page"])
        page = json.load(open(os.path.join(tiny, "net_probe.json"), encoding="utf-8"))["results"][0]
        self.assertTrue(page["truncated"])
        self.assertEqual(page["bytes"], 120)
        self.assertGreater(page["bytes_original"], 120)


class PublishReportTests(unittest.TestCase):
    """Публикация отчёта: ветка данных не должна затягивать код проекта."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git недоступен в этом окружении")
        self.tmp = tempfile.mkdtemp(prefix="publish-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git(["init", "-q", "-b", "main", "."], cwd=self.repo)
        with open(os.path.join(self.repo, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("# код проекта\n")
        with open(os.path.join(self.repo, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# код проекта\n")
        self._git(["add", "."], cwd=self.repo)
        self._git(["commit", "-qm", "init"], cwd=self.repo)

        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "net_probe.json"), "w", encoding="utf-8") as fh:
            json.dump({"task": "net_probe", "ok": True, "results": []}, fh)

    def _git(self, args, cwd):
        proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                   "GIT_AUTHOR_EMAIL": "t@example.com",
                                   "GIT_COMMITTER_NAME": "t",
                                   "GIT_COMMITTER_EMAIL": "t@example.com"})
        if proc.returncode != 0 and args[0] not in ("init",):
            raise AssertionError(f"git {args} -> {proc.returncode}\n{proc.stderr}")
        return proc

    def _publish(self, **overrides):
        args = {
            "repo_dir": self.repo,
            "data_dir": self.data,
            "template_dir": TEMPLATES,
            "task": "net_probe",
            "no_push": True,
        }
        args.update(overrides)
        argv = []
        for key, value in args.items():
            argv.append("--" + key.replace("_", "-"))
            if value is not True:
                argv.append(str(value))
        return publish_report.main(argv)

    def test_publishes_only_data_into_new_branch(self):
        self.assertEqual(self._publish(), 0)
        files = self._git(["ls-tree", "-r", "--name-only", "ci-reports"], self.repo).stdout.split()
        self.assertEqual(sorted(files),
                         sorted([".gitattributes", "README.md", "data/net_probe.json"]))
        self.assertNotIn("app.py", files)

    def test_second_publish_has_nothing_to_commit(self):
        self.assertEqual(self._publish(), 0)
        self.assertEqual(self._publish(), 0)
        count = self._git(["rev-list", "--count", "ci-reports"], self.repo).stdout.strip()
        self.assertEqual(count, "1")

    def test_missing_report_becomes_placeholder(self):
        shutil.rmtree(self.data)
        self.assertEqual(self._publish(), 0)
        shown = self._git(["show", "ci-reports:data/net_probe.json"], self.repo).stdout
        self.assertIn('"ok": false', shown)


if __name__ == "__main__":
    unittest.main()
