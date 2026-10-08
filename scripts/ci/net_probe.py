#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""net_probe - снимает реальные HTTP-ответы с площадок проекта и складывает отчёт.

Зачем это нужно
---------------
Агент работает в песочнице, из которой доступны только github.com,
api.github.com и codeload.github.com. Ни pornolab.net, ни xxxclub.to, ни Knaben,
ни внешний PLAY'A API ему недоступны, а логи и артефакты GitHub Actions скачать
нельзя (results-receiver / pipelines заблокированы). Поэтому единственный способ
показать агенту реальный ответ сайта - выполнить эту задачу на self-hosted
раннере (компьютер пользователя) и закоммитить результат в ветку ci-reports,
откуда агент читает его через git fetch.

Что сохраняется
---------------
    data/net_probe.json                сводка: статусы, заголовки, размеры, sha256
    data/net_probe__<name>.<ext>.gz    тело ответа (gzip), перезаписывается каждый запуск

Старые версии тела никуда не деваются - они остаются в истории git ветки
ci-reports, поэтому отдельная ротация файлов не нужна.

Правила безопасности (сознательно узкие)
----------------------------------------
  * разрешены только методы GET и POST;
  * хост каждой цели должен быть перечислен в allowed_hosts файла целей,
    иначе задача падает, не сделав ни одного запроса;
  * никаких cookies: задача НЕ читает forum_cookies_*.txt, .env, базу или
    что-либо ещё из рабочего клона пользователя - только свой файл целей;
  * в отчёт попадают только заголовки из KEEP_HEADERS, поэтому Set-Cookie,
    Cookie и Authorization не могут утечь в принципе;
  * из тел вырезаются типовые секреты (см. REDACTIONS), счётчик срабатываний
    пишется в отчёт;
  * тело обрезается до --max-body байт и упаковывается в gzip.

Запуск
------
    python scripts/ci/net_probe.py --targets scripts/ci/targets.json --out-dir <каталог>

Коды возврата
-------------
    0 - отчёт записан (даже если часть целей не ответила: это тоже информация);
    2 - отчёт записать не удалось (нет файла целей, битый JSON, нет requests).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

try:
    import requests
except ImportError:  # pragma: no cover - зависит от окружения раннера
    requests = None

DEFAULT_UA = "deovr-bridge/1.0 (+net_probe)"
DEFAULT_MAX_BODY = 512 * 1024
DEFAULT_KEEP_HISTORY = 10

# Только эти заголовки вообще попадают в отчёт: всё прочее (в том числе
# Set-Cookie / Cookie / Authorization) отбрасывается на уровне выборки.
KEEP_HEADERS = (
    "content-type",
    "content-length",
    "server",
    "location",
    "x-powered-by",
    "retry-after",
    "cache-control",
)

# (регулярка, замена). Регулярки намеренно НЕ трогают «длинные произвольные
# строки»: infohash, magnet-ссылки и имена файлов - это ровно то, ради чего
# задача существует, их затирать нельзя. Режем только именованные секреты.
REDACTIONS = (
    (
        re.compile(
            rb"(?i)\b(phpsessid|sessionid|session_id|bb_data|bb_sessionhash|"
            rb"bb_password|password|passwd|pwd|token|access_token|refresh_token|"
            rb"api[_-]?key|apikey|secret)=([^&\s;\"'<>]{3,})"
        ),
        rb"\1=[REDACTED]",
    ),
    (re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"), b"Bearer [REDACTED]"),
    (
        re.compile(
            rb"(?is)(name=[\"']?[a-z0-9_\-]*(?:token|sess|secret|password|passwd|"
            rb"auth|hash|key|bb_data)[a-z0-9_\-]*[\"']?[^>]{0,300}?value=[\"'])"
            rb"([^\"']{8,})([\"'])"
        ),
        rb"\1[REDACTED]\3",
    ),
)

# Маркеры, по которым агент сразу видит, что вместо страницы отдали заглушку.
SIGNAL_MARKERS = (
    ("antibot", ("antibot",)),
    ("captcha", ("captcha", "recaptcha", "hcaptcha")),
    ("cloudflare", ("cloudflare", "checking your browser", "cf-chl")),
    ("ddos-guard", ("ddos-guard",)),
    ("access-denied", ("access denied", "доступ запрещён", "403 forbidden")),
    ("login-page", ("login.php", "you must login", "войти на форум")),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _load_targets(path: str) -> list:
    """Читает и валидирует список целей. Бросает ValueError при любой проблеме."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    allowed = {str(host).lower() for host in (data.get("allowed_hosts") or [])}
    raw_targets = data.get("targets") or []
    if not allowed:
        raise ValueError("targets.json: пустой allowed_hosts")
    if not raw_targets:
        raise ValueError("targets.json: пустой список targets")

    prepared = []
    seen_names = set()
    for raw in raw_targets:
        name = str(raw.get("name") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not name:
            raise ValueError("targets.json: у цели нет name")
        if name in seen_names:
            raise ValueError(f"targets.json: имя цели повторяется: {name}")
        seen_names.add(name)

        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"targets.json: некорректный URL: {url!r}")
        if parts.hostname.lower() not in allowed:
            raise ValueError(f"targets.json: хост {parts.hostname} не в allowed_hosts")

        method = str(raw.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            raise ValueError(f"targets.json: метод {method} не разрешён (только GET/POST)")

        prepared.append(
            {
                "name": name,
                "url": url,
                "method": method,
                "json": raw.get("json"),
                "data": raw.get("data"),
                "headers": dict(raw.get("headers") or {}),
                "impersonate": raw.get("impersonate"),
                "note": str(raw.get("note") or ""),
            }
        )
    return prepared


def _session_for(target):
    """curl_cffi нужен только там, где сайт проверяет TLS-отпечаток."""
    impersonate = target.get("impersonate")
    if impersonate:
        try:
            from curl_cffi import requests as cffi_requests

            return cffi_requests.Session(impersonate=impersonate)
        except Exception as exc:  # noqa: BLE001 - важен сам факт отката
            print(f"[net_probe] curl_cffi недоступен ({exc}), берём requests", file=sys.stderr)
    return requests.Session()


def _decode(data: bytes, content_type: str) -> str:
    """Декодирует тело для извлечения title и поиска маркеров. Никогда не падает."""
    charset = ""
    match = re.search(r"charset=[\"']?([\w\-]+)", content_type or "")
    if match:
        charset = match.group(1)
    if not charset:
        match = re.search(rb"(?i)charset=[\"']?([\w\-]+)", data[:4096])
        if match:
            charset = match.group(1).decode("ascii", "replace")
    for encoding in (charset, "utf-8", "cp1251"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _extract_title(text: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()[:300]


def _detect_signals(text: str) -> list:
    head = text[:40000].casefold()
    return [name for name, markers in SIGNAL_MARKERS if any(m in head for m in markers)]


def _redact(data: bytes) -> tuple:
    hits = 0
    for pattern, replacement in REDACTIONS:
        data, count = pattern.subn(replacement, data)
        hits += count
    return data, hits


def _body_ext(content_type: str) -> str:
    low = (content_type or "").lower()
    if "json" in low:
        return "json"
    if "html" in low or "xml" in low or "text" in low:
        return "html"
    return "bin"


def _probe(target: dict, max_body: int, timeout: float, out_dir: str) -> dict:
    """Выполняет один запрос и возвращает запись результата (никогда не бросает)."""
    result = {
        "name": target["name"],
        "url": target["url"],
        "method": target["method"],
        "note": target["note"],
        "status": None,
        "reason": "",
        "final_url": "",
        "elapsed_ms": None,
        "content_type": "",
        "bytes": None,
        "bytes_original": None,
        "body_file": "",
        "sha256": "",
        "redactions": 0,
        "title": "",
        "signals": [],
        "headers": {},
        "error": None,
    }

    headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    headers.update({str(k): str(v) for k, v in (target.get("headers") or {}).items()})

    kwargs = {"timeout": timeout, "allow_redirects": True}
    if target["method"] == "POST":
        if target.get("json") is not None:
            kwargs["json"] = target["json"]
        if target.get("data") is not None:
            kwargs["data"] = target["data"]

    started = time.time()
    try:
        session = _session_for(target)
        response = session.request(target["method"], target["url"], headers=headers, **kwargs)
        body = response.content or b""
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        result["status"] = response.status_code
        result["reason"] = str(response.reason or "")
        result["final_url"] = str(response.url)
        result["content_type"] = str(response.headers.get("content-type") or "")
        result["headers"] = {
            key: str(value)
            for key, value in response.headers.items()
            if key.lower() in KEEP_HEADERS
        }
    except Exception as exc:  # noqa: BLE001 - ошибка сети это тоже результат
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["bytes_original"] = len(body)
    if len(body) > max_body:
        body = body[:max_body]
        result["truncated"] = True

    redacted, hits = _redact(body)
    result["redactions"] = hits
    result["bytes"] = len(redacted)
    result["sha256"] = hashlib.sha256(redacted).hexdigest()

    body_file = f"net_probe__{target['name']}.{_body_ext(result['content_type'])}.gz"
    with gzip.open(os.path.join(out_dir, body_file), "wb") as fh:
        fh.write(redacted)
    result["body_file"] = body_file

    text = _decode(redacted, result["content_type"])
    result["title"] = _extract_title(text)
    result["signals"] = _detect_signals(text)
    return result


def _load_history(summary_path: str) -> dict:
    try:
        with open(summary_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        history = data.get("history")
        return history if isinstance(history, dict) else {}
    except (OSError, ValueError):
        return {}


def _append_history(history: dict, result: dict, keep: int) -> None:
    entries = history.setdefault(result["name"], [])
    entries.append(
        {
            "ts": _now_iso(),
            "status": result["status"],
            "bytes": result["bytes"],
            "sha256": (result["sha256"] or "")[:12],
            "error": result["error"],
        }
    )
    del entries[:-max(keep, 1)]


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Снять реальные HTTP-ответы площадок в отчёт")
    parser.add_argument("--targets", required=True, help="файл списка целей (JSON)")
    parser.add_argument("--out-dir", required=True, help="каталог для отчёта")
    parser.add_argument("--max-body", type=int, default=DEFAULT_MAX_BODY)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep-history", type=int, default=DEFAULT_KEEP_HISTORY)
    parser.add_argument("--only", default="", help="имена целей через запятую (для локальной отладки)")
    args = parser.parse_args(argv)

    if requests is None:
        print("net_probe: не установлен пакет requests", file=sys.stderr)
        return 2

    try:
        targets = _load_targets(args.targets)
    except (OSError, ValueError) as exc:
        print(f"net_probe: список целей не прочитан: {exc}", file=sys.stderr)
        return 2

    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        targets = [t for t in targets if t["name"] in wanted]
        if not targets:
            print("net_probe: --only не совпал ни с одной целью", file=sys.stderr)
            return 2

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    summary_path = os.path.join(out_dir, "net_probe.json")
    history = _load_history(summary_path)

    started_at = _now_iso()
    results = []
    for target in targets:
        print(f"[net_probe] {target['name']}: {target['method']} {target['url']}")
        result = _probe(target, args.max_body, args.timeout, out_dir)
        _append_history(history, result, args.keep_history)
        results.append(result)
        status = result["status"] if result["status"] is not None else "нет ответа"
        print(
            f"[net_probe]   -> {status} {result['reason']} "
            f"{result['bytes'] or 0}b title={result['title'][:60]!r} "
            f"signals={','.join(result['signals']) or '-'} error={result['error'] or '-'}"
        )

    payload = {
        "task": "net_probe",
        "schema": 1,
        "ref": _env("GITHUB_REF_NAME"),
        "sha": _env("GITHUB_SHA"),
        "run_id": _env("GITHUB_RUN_ID"),
        "runner": _env("RUNNER_NAME") or _env("COMPUTERNAME"),
        "started_at": started_at,
        "finished_at": _now_iso(),
        "ok": all(r["status"] is not None and not r["error"] for r in results),
        "results": results,
        "history": history,
    }
    try:
        _write_json(summary_path, payload)
    except OSError as exc:
        print(f"net_probe: отчёт не записан: {exc}", file=sys.stderr)
        return 2

    print(f"[net_probe] отчёт: {summary_path} (ok={payload['ok']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
