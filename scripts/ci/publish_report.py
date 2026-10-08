#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Публикует отчёт задачи в ветку данных (по умолчанию ci-reports).

Почему это Python, а не PowerShell в workflow: эту логику нужно проверять на
настоящем git-репозитории, а «клей» в workflow проверить нельзя. Скрипт умеет
работать полностью локально (--no-push), поэтому он покрыт прогоном на стенде:
создание ветки с нуля, обновление существующей, отсутствие изменений.

Что делает:
  1. если каталога с отчётом нет - создаёт заглушку с текстом ошибки, чтобы
     агент всё равно узнал, что задача не отработала;
  2. забирает ветку данных с origin; если её нет - создаёт orphan-ветку;
  3. копирует отчёт в data/ и шаблонные файлы в корень;
  4. коммитит и пушит строго перечисленные пути.

Работает в рабочем клоне раннера: смена ветки и коммит затрагивают только этот
одноразовый клон, а не рабочий каталог пользователя.

Коды возврата: 0 - всё в порядке (в том числе «нечего публиковать»),
              2 - публикация не удалась.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

DEFAULT_BRANCH = "ci-reports"


def _run(args: list, cwd: str, check: bool = True, quiet: bool = False) -> int:
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not quiet:
        print(f"[publish] $ {' '.join(args)}")
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print(proc.stderr.rstrip())
    if check and proc.returncode != 0:
        raise RuntimeError(f"команда завершилась с кодом {proc.returncode}: {' '.join(args)}")
    return proc.returncode


def _read_templates(template_dir: str) -> dict:
    """Шаблонные файлы ветки отчётов: имя -> байты."""
    templates = {}
    if not os.path.isdir(template_dir):
        print(f"[publish] предупреждение: нет каталога шаблонов {template_dir}")
        return templates
    for name in sorted(os.listdir(template_dir)):
        path = os.path.join(template_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                templates[name] = fh.read()
    return templates


def _write_placeholder(data_dir: str, task: str, error: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    payload = {
        "task": task or "unknown",
        "schema": 1,
        "ok": False,
        "ref": os.environ.get("GITHUB_REF_NAME", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "runner": os.environ.get("RUNNER_NAME", ""),
        "error": error or "Задача не оставила отчёт.",
        "results": [],
    }
    with open(os.path.join(data_dir, f"{task or 'task'}.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"[publish] отчёт отсутствовал - записана заглушка с текстом ошибки")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Опубликовать отчёт в ветку данных")
    parser.add_argument("--repo-dir", required=True, help="рабочий клон раннера")
    parser.add_argument("--data-dir", required=True, help="каталог, куда задача положила отчёт")
    parser.add_argument("--template-dir", required=True, help="шаблон файлов ветки отчётов")
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--task", default="")
    parser.add_argument("--error", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--no-push", action="store_true", help="локальный режим, без push")
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo_dir)
    data_dir = os.path.abspath(args.data_dir)
    template_dir = os.path.abspath(args.template_dir)

    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"[publish] это не git-репозиторий: {repo}", file=sys.stderr)
        return 2

    try:
        # 1. Отчёт должен существовать И быть непустым: пустой каталог означает,
        #    что задача упала до записи, и тогда агент не узнал бы о сбое.
        if not os.path.isdir(data_dir) or not os.listdir(data_dir):
            _write_placeholder(data_dir, args.task, args.error)

        # 2. Шаблоны читаем заранее: после смены ветки рабочее дерево станет другим.
        templates = _read_templates(template_dir)

        # 3. Ветка данных: берём из origin или создаём orphan-ветку.
        rc = _run(
            ["git", "fetch", "--depth", "1", "origin", args.branch],
            repo,
            check=False,
            quiet=True,
        )
        print(f"[publish] git fetch origin {args.branch}: rc={rc}")

        if rc == 0:
            _run(["git", "checkout", "-f", "-B", args.branch, "FETCH_HEAD"], repo, quiet=True)
            print(f"[publish] ветка {args.branch} получена из origin")
        elif _run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{args.branch}"],
                  repo, check=False, quiet=True) == 0:
            # origin недоступен, но ветка уже есть локально - продолжаем на ней,
            # чтобы не порождать вторую, несвязанную историю отчётов.
            _run(["git", "checkout", "-f", args.branch], repo, quiet=True)
            print(f"[publish] origin недоступен: продолжаем на локальной ветке {args.branch}")
        else:
            _run(["git", "checkout", "--orphan", args.branch], repo, quiet=True)
            # Индекс ещё содержит файлы исходной ветки - очищаем его, рабочее
            # дерево не трогаем.
            _run(["git", "rm", "-r", "--cached", ".", "--quiet"], repo, check=False, quiet=True)
            print(f"[publish] ветки {args.branch} не было - создаём orphan-ветку")

        _run(["git", "config", "user.name", "deovr-ci"], repo, quiet=True)
        _run(["git", "config", "user.email", "ci@users.noreply.github.com"], repo, quiet=True)

        # 4. Данные: каталог приводим к текущему набору целей, чтобы удалённые
        #    цели не оставались в ветке навсегда.
        target_data = os.path.join(repo, "data")
        if os.path.isdir(target_data):
            shutil.rmtree(target_data)
        shutil.copytree(data_dir, target_data)

        for name, content in templates.items():
            with open(os.path.join(repo, name), "wb") as fh:
                fh.write(content)

        # 5. Добавляем строго перечисленные пути: в orphan-режиме в рабочем
        #    дереве ещё лежат файлы исходной ветки, и 'git add -A' затащил бы их.
        _run(["git", "add", "-A", "--", "data"], repo, quiet=True)
        if templates:
            _run(["git", "add", "-A", "--"] + sorted(templates), repo, quiet=True)

        if _run(["git", "diff", "--cached", "--quiet"], repo, check=False, quiet=True) == 0:
            print("[publish] изменений нет - публиковать нечего")
            return 0

        message = args.message or f"{args.task or 'task'}: отчёт с раннера"
        _run(["git", "commit", "--quiet", "-m", message], repo, quiet=True)
        print(f"[publish] коммит создан: {message}")

        if args.no_push:
            print("[publish] режим --no-push: push пропущен")
            return 0

        _run(["git", "push", "origin", f"HEAD:refs/heads/{args.branch}"], repo, quiet=True)
        print(f"[publish] отчёт опубликован в ветку {args.branch}")
        return 0
    except (OSError, RuntimeError, shutil.Error) as exc:
        print(f"[publish] ошибка публикации: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
