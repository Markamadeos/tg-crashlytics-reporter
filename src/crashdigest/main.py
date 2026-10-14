"""Точка входа: сборка зависимостей, один прогон, режим демона."""
from __future__ import annotations

import argparse
import html
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from crashdigest import analysis, digest, markdown as md
from crashdigest.auth import TokenProvider
from crashdigest.config import Config, ConfigError, load
from crashdigest.crashlytics import CrashlyticsClient, CrashlyticsError, IssueRow
from crashdigest.digest import render_weekly, week_totals, weekly_growth
from crashdigest.llm import LlmClient
from crashdigest.scheduler import (
    install_sigterm_handler,
    next_run_at,
    resolved_zone_name,
    run_forever,
)
from crashdigest.state import State
from crashdigest.telegram import Telegram
from crashdigest.versions import RecentVersions, recent_versions

BOOTSTRAP_WINDOW = timedelta(days=90)
WEEK = timedelta(days=7)
DAY = timedelta(days=1)


@dataclass
class Deps:
    crashlytics: object
    state: State
    telegram: object
    # None, когда разбор выключен: без ключа шлюза контейнер работает ровно
    # как до появления разбора.
    llm: object | None = None


def build_deps(cfg: Config, *, telegram: object | None = None) -> Deps:
    """`telegram`, если передан, используется как есть — так main() может
    собрать отправителя раньше State/CrashlyticsClient и сообщить об их
    отказе тем же каналом, а не голым traceback.
    """
    tokens = TokenProvider(cfg.credentials_path)
    return Deps(
        crashlytics=CrashlyticsClient(cfg.project, cfg.app_id, tokens),
        state=State(cfg.state_path),
        telegram=telegram if telegram is not None else Telegram(
            cfg.bot_token, cfg.chat_id, proxy=cfg.proxy
        ),
        llm=LlmClient(
            cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model, cfg.llm_timeout
        ) if cfg.analysis_enabled else None,
    )


def _log(message: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}",
          flush=True)


def run_once(cfg: Config, deps: Deps, *, now: datetime, bootstrap: bool = False) -> None:
    """Один прогон. Любой отказ уходит в канал ровно один раз и пробрасывается.

    Единая точка отчёта: неважно, отказал ли Crashlytics, SQLite (диск полон,
    нет прав, база повреждена) или сама доставка — молчание хуже любого
    сообщения об ошибке, а дублировать try/except в каждом внутреннем шаге
    было бы источником как раз таких пропусков.
    """
    try:
        _run(cfg, deps, now=now, bootstrap=bootstrap)
    except Exception as exc:  # noqa: BLE001 — молчащий отказ здесь хуже любого другого
        detail = f"{type(exc).__name__}: {exc}"
        _log(f"прогон завершился ошибкой: {detail}")
        _notify_failure(deps, "Дайджест не собран", detail)
        raise


def _run(cfg: Config, deps: Deps, *, now: datetime, bootstrap: bool = False) -> None:
    if bootstrap:
        start, end, truncated = now - BOOTSTRAP_WINDOW, now, False
    else:
        start, end, truncated = deps.state.window(now)

    _log(f"окно {start.isoformat()} → {end.isoformat()} (обрезано={truncated})")

    # Версии считаем и для bootstrap: иначе 90-дневное окно инициализации
    # запомнило бы issue всех версий подряд, что навсегда лишает фильтр его
    # смысла — старая версия больше никогда не попадёт в «новое».
    versions = _tracked_versions(cfg, deps, now=now)

    if bootstrap:
        rows = deps.crashlytics.top_issues(
            start, end, cfg.error_types, versions=list(versions.display_names)
        )
        # Доставка сначала, состояние — только после подтверждённой отправки:
        # тот же инвариант, что и в основном пути ниже, иначе неудачная
        # отправка молча продвинет окно и первые крэши пропадут без следа.
        deps.telegram.send(digest.render_bootstrap(len(rows), cfg.app_name, list(versions.display_versions)))
        deps.state.record(rows, now)
        deps.state.mark_success(now)
        _log(f"bootstrap: запомнено {len(rows)} issue")
        return

    rows = deps.crashlytics.top_issues(
        start, end, cfg.error_types, versions=list(versions.display_names)
    )

    new_rows = deps.state.diff_new(rows)
    messages = digest.render(
        new_rows, start, end,
        app_name=cfg.app_name, project=cfg.project, app_id=cfg.app_id,
        scanned=len(rows),
        versions=list(versions.display_versions),
    )

    # Курсор обновлений снимается ДО отправки: он отвечает на вопрос «что
    # пришло после нашего поста», и снятый после — уже может пропустить
    # автофорвард, если тот успел прийти первым. И только когда разбирать
    # есть что: в тихие сутки это был бы ежедневный запрос впустую, который
    # к тому же умеет отказывать и шуметь в логах.
    cursor = _updates_cursor(cfg, deps) if new_rows else None

    sent = deps.telegram.send(messages)   # сначала доставка…
    deps.state.record(rows, now)
    deps.state.mark_success(now)          # …и только потом сдвиг окна
    _log(f"отправлено: новых {len(new_rows)} из {len(rows)} в окне")

    # Разбор — надстройка над уже доставленным постом и уже зафиксированным
    # успехом, поэтому его отказ, как и отказ недельного отчёта, не отменяет
    # ни того, ни другого.
    if cfg.analysis_enabled and new_rows:
        try:
            _analyze_and_comment(
                cfg, deps, rows=new_rows, post_id=_first_post_id(sent),
                cursor=cursor, start=start, end=end,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"разбор крашей не собран: {type(exc).__name__}: {exc}")
            _notify_failure(deps, "Разбор крашей не собран",
                            f"{type(exc).__name__}: {exc}")

    # Недельный отчёт — дополнение. Его отказ не должен отменять уже
    # доставленный дневной дайджест и уже зафиксированный успех.
    if _weekly_due(deps.state.last_weekly_at(), now, cfg.weekly_weekday):
        try:
            _weekly_report(cfg, deps, now=now, versions=versions)
            deps.state.mark_weekly_sent(now)
        except Exception as exc:  # noqa: BLE001
            _log(f"недельный отчёт не собран: {type(exc).__name__}: {exc}")
            _notify_failure(deps, "Недельный отчёт не собран", f"{type(exc).__name__}: {exc}")


def _tracked_versions(cfg: Config, deps: Deps, *, now: datetime) -> RecentVersions:
    """N последних версий приложения.

    topVersions — отчёт за интервал: он возвращает только версии, у которых
    были события внутри окна запроса. Поэтому список версий ОТ ДЛИНЫ ОКНА
    ЗАВИСИТ — суточное окно может не увидеть свежий низкотрафичный релиз.
    Берём каталог за 7 дней — тот же единственный запрос, но заметно
    устойчивее к этому эффекту.
    """
    known = deps.crashlytics.list_versions(now - timedelta(days=7), now)
    recent = recent_versions(known, cfg.version_window)
    if not recent.display_names:
        raise CrashlyticsError(
            "topVersions не вернул ни одной версии — фильтр применить не к чему. "
            "Прогон без фильтра дал бы числа по всем версиям и записал бы старые "
            "issue в состояние, поэтому останавливаемся."
        )
    return recent


def _weekly_due(last_weekly: datetime | None, now: datetime, weekday: int) -> bool:
    """Пора ли слать недельный отчёт.

    Одного условия «сегодня понедельник» мало в обе стороны: выключенный в
    понедельник NAS терял отчёт за неделю бесследно, а перезапуск в
    понедельник слал его дважды — сначала догоняющим прогоном, потом
    плановым. Догоняющая отправка не смещает день: следующий плановый
    понедельник всё равно наступит.
    """
    if last_weekly is None:
        return now.weekday() == weekday
    if now.weekday() == weekday:
        # В нужный день шлём, если сегодня ещё не слали. Без этой ветки
        # догоняющая отправка во вторник переносила бы якорь на вторник
        # навсегда: следующий понедельник наступает через шесть дней, а
        # «прошла неделя» требует семи.
        return now - last_weekly >= DAY
    return now - last_weekly >= WEEK


def _weekly_report(cfg: Config, deps: Deps, *, now: datetime,
                   versions: RecentVersions) -> None:
    """Отдельное сообщение о росте фона. Дополнение к дневному, а не замена."""
    names = list(versions.display_names)
    current = (now - WEEK, now)
    previous = (now - WEEK - WEEK, now - WEEK)

    cur = deps.crashlytics.top_issues(*current, cfg.error_types, versions=names)
    prev = deps.crashlytics.top_issues(*previous, cfg.error_types, versions=names)

    deps.telegram.send(
        render_weekly(
            weekly_growth(cur, prev),
            app_name=cfg.app_name,
            project=cfg.project,
            app_id=cfg.app_id,
            versions=list(versions.display_versions),
            previous=previous,
            current=current,
            prev_totals=week_totals(prev),
            cur_totals=week_totals(cur),
            top=cfg.weekly_top,
        )
    )


def _first_post_id(sent) -> int | None:
    """id дневного поста. `render` отдаёт одно сообщение, но id берём первый
    осознанно: комментарии вешаются под началом дайджеста, а не под хвостом.
    """
    for item in sent or []:
        if isinstance(item, int):
            return item
    return None


def _updates_cursor(cfg: Config, deps: Deps) -> int | None:
    """Курсор обновлений или None, если снять не удалось.

    Отказ здесь НЕ должен мешать дайджесту: getUpdates нужен только разбору, а
    пост с таблицей обязан уйти даже при висящем webhook'е.
    """
    if not cfg.analysis_enabled:
        return None
    try:
        return deps.telegram.updates_cursor()
    except Exception as exc:  # noqa: BLE001
        _log(f"не удалось снять курсор обновлений: {type(exc).__name__}: {exc}")
        return None


def _analyze_and_comment(cfg: Config, deps: Deps, *, rows, post_id: int | None,
                         cursor: int | None, start: datetime,
                         end: datetime) -> None:
    """Комментарии с разбором под дневным постом."""
    if post_id is None:
        raise RuntimeError(
            "Telegram не вернул id поста — не к чему привязать комментарии"
        )

    linked = deps.telegram.linked_chat_id()
    if linked is None:
        raise RuntimeError(
            "у канала нет связанной группы обсуждений: комментировать посты "
            "негде. Привяжите группу к каналу и добавьте бота в неё админом."
        )

    reply_to = deps.telegram.find_discussion_copy(post_id, cursor=cursor)
    if reply_to is None:
        raise RuntimeError(
            f"копия поста {post_id} не появилась в группе обсуждений за "
            "отведённое время. Обычная причина — бот не админ в группе "
            "обсуждений и не видит её сообщений."
        )

    # Сортировка явная, хотя отчёт и так приходит по убыванию событий:
    # «разбираем топ-N по массовости» — требование, и оно не должно зависеть
    # от порядка, в котором Crashlytics решит отдать строки.
    listed = sorted(rows, key=lambda r: r.events_count, reverse=True)[:cfg.analyze_top]
    _log(f"разбор: {len(listed)} из {len(rows)} новых, модель {cfg.llm_model}")
    for row in listed:
        try:
            _comment_one(cfg, deps, row=row, start=start, end=end,
                         chat_id=linked, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            # Поштучно и без сообщения в канал: один сломавшийся разбор не
            # должен лишить разбора остальные и не стоит отдельной паники в
            # канале — пост с таблицей уже на месте.
            _log(f"разбор {row.issue_id} не собран: {type(exc).__name__}: {exc}")


def _comment_one(cfg: Config, deps: Deps, *, row: IssueRow, start: datetime,
                 end: datetime, chat_id: str, reply_to: int) -> None:
    text = _render_analysis(cfg, deps, row=row, start=start, end=end)
    if text is None:
        return
    deps.telegram.send_comment(text, chat_id=chat_id, reply_to=reply_to)
    _log(f"разбор {row.issue_id} отправлен")


def _render_analysis(cfg: Config, deps: Deps, *, row: IssueRow,
                     start: datetime, end: datetime) -> str | None:
    """Готовый markdown разбора или None, если разбирать нечего."""
    event = deps.crashlytics.sample_event(
        row.issue_id, start, end, context=cfg.crash_context
    )
    if event is None:
        _log(f"разбор {row.issue_id}: событий в окне нет, пропускаем")
        return None

    payload = deps.llm.json_object(
        analysis.SYSTEM_PROMPT, analysis.build_prompt(event, row)
    )
    verdict = analysis.parse_answer(payload)
    return analysis.render_comment(
        verdict, event, row, project=cfg.project, app_id=cfg.app_id,
        model=cfg.llm_model,
    )


def _analyze_one(cfg: Config, deps: Deps, issue_id: str, *, now: datetime,
                 dry_run: bool) -> int:
    """Разбор одного issue по требованию: отладочный путь для --analyze.

    Окно берётся широкое: issue, который разбирают руками, почти никогда не
    из последних суток, и узкое окно не нашло бы ни одного его события.
    """
    start, end = now - BOOTSTRAP_WINDOW, now
    row = _find_row(cfg, deps, issue_id, start=start, end=end)

    text = _render_analysis(cfg, deps, row=row, start=start, end=end)
    if text is None:
        _log(f"разбор {issue_id}: событий за {BOOTSTRAP_WINDOW.days} дней нет")
        return 1

    if dry_run:
        print(text)
        return 0
    deps.telegram.send([text])
    return 0


def _find_row(cfg: Config, deps: Deps, issue_id: str, *, start: datetime,
              end: datetime) -> IssueRow:
    """Строка отчёта по issue_id — ради настоящих чисел в разборе.

    Без фильтра по версиям: разбирают руками как раз то, чего нет в текущих
    отслеживаемых версиях. Если issue в отчёте не нашёлся, собираем
    минимальную строку — разбор по стектрейсу возможен и без чисел.
    """
    try:
        for row in deps.crashlytics.top_issues(start, end, cfg.error_types):
            if row.issue_id == issue_id:
                return row
    except CrashlyticsError as exc:
        _log(f"не удалось получить строку отчёта: {exc}")
    _log(f"issue {issue_id} в отчёте не найден — числа в разборе будут нулевые")
    return IssueRow(
        issue_id=issue_id, title="", subtitle="", error_type="?", state="?",
        last_seen_version="?", first_seen_version="?",
        events_count=0, impacted_users=0,
    )


def _notify_failure(deps: Deps, headline: str, detail: str) -> None:
    """Пытается сообщить о поломке в тот же канал. Молчание — худший исход.

    Через send_plain, а не send: если отказал именно rich-формат, сообщить
    об этом сможет только старый метод.
    """
    try:
        deps.telegram.send_plain(digest.render_error(headline, detail))
    except Exception as exc:  # noqa: BLE001
        _log(f"не удалось сообщить об ошибке в Telegram: {exc}")


def _startup_text(
    cfg: Config, *, zone: str, now: datetime, last_success: datetime | None
) -> str:
    """Текст стартового сообщения. Чистая функция: зона и время приходят
    снаружи, поэтому проверяется без реального Telegram и часов.
    """
    next_text = next_run_at(now, cfg.schedule).isoformat(timespec="seconds")
    if last_success is None:
        last_text = "ещё не было"
    else:
        last_text = last_success.astimezone(now.tzinfo).isoformat(timespec="seconds")
    rows = [
        ["🕐 Зона", md.esc(zone)],
        ["⏱ Расписание", f"`{cfg.schedule}`"],
        ["▶️ Следующий прогон", next_text],
        ["✅ Последний успешный", last_text],
    ]
    if cfg.analysis_enabled:
        # Модель задаётся параметром compose, а не кодом: контейнер, молча
        # поднявшийся не с той моделью, иначе ничем себя не выдаёт — ровно та
        # же причина, по которой здесь печатается разрешённая зона.
        # Без md.esc внутри обратных кавычек — ровно как строкой выше у
        # расписания: внутри code-span разметки нет, а экранирующий слэш
        # показался бы получателю как слэш.
        rows.append(["🤖 Разбор",
                     f"`{cfg.llm_model}`, до {cfg.analyze_top} на прогон"])

    return "\n".join([
        f"# 🚀 {md.esc(cfg.app_name)} — контейнер запущен",
        "",
        md.table(("", ""), rows, "ll"),
    ])


def _send_startup_message(cfg: Config, deps: Deps, *, now: datetime) -> None:
    """Каждый передеплой становится самопроверяемым: видно зону, расписание,
    следующий прогон и когда был последний успешный. Молчаливый откат TZ на
    UTC иначе никак не заметен. Отказ отправки не должен мешать старту.
    """
    text = _startup_text(
        cfg, zone=resolved_zone_name(), now=now,
        last_success=deps.state.last_success_at(),
    )
    try:
        deps.telegram.send([text])
    except Exception as exc:  # noqa: BLE001 — стартовое сообщение не блокирует запуск
        _log(f"не удалось отправить стартовое сообщение: {exc}")


def _catch_up_due(last_success: datetime | None, now: datetime, schedule: str) -> bool:
    """Пора ли досрочно прогнать задачу при старте демона.

    Если с последнего успеха прошло больше периода расписания — редеплой
    или перезагрузка NAS иначе откладывали бы дайджест на срок до суток, а
    сломанный деплой обнаруживался бы только в плановый час.
    """
    return last_success is None or now >= next_run_at(last_success, schedule)


def _run_startup_catch_up(cfg: Config, deps: Deps, *, now: datetime) -> None:
    """Догоняющий прогон при старте демона. Обязан не мешать запуску: любой
    отказ здесь уже ушёл в канал из run_once — здесь его достаточно
    залогировать и дать циклу планировщика стартовать как обычно.
    """
    if not _catch_up_due(deps.state.last_success_at(), now, cfg.schedule):
        return
    _log("догоняющий прогон при старте: с последнего успеха прошло больше периода")
    try:
        run_once(cfg, deps, now=now)
    except Exception as exc:  # noqa: BLE001
        _log(f"догоняющий прогон при старте не удался: {exc}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crashdigest")
    parser.add_argument("--once", action="store_true",
                        help="один прогон и выход")
    parser.add_argument("--bootstrap", action="store_true",
                        help="запомнить текущие issue за 90 дней, ничего не перечисляя")
    parser.add_argument("--analyze", metavar="ISSUE_ID",
                        help="разобрать один issue по стектрейсу и выйти")
    parser.add_argument("--dry-run", action="store_true",
                        help="с --analyze: напечатать разбор в stdout, ничего не отправляя")
    args = parser.parse_args(argv)

    try:
        cfg = load(os.environ)
    except ConfigError as exc:
        _log(f"конфигурация неверна: {exc}")
        return 2

    # Telegram собирается первым и не зависит ни от чего, что может отказать
    # при старте (например, /data смонтирован read-only). build_deps ниже
    # может упасть — и тогда именно этот экземпляр донесёт причину в канал,
    # а не голый traceback + перезапуск контейнера по кругу.
    tg = Telegram(cfg.bot_token, cfg.chat_id, proxy=cfg.proxy)
    try:
        deps = build_deps(cfg, telegram=tg)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        _log(f"не удалось запуститься: {detail}")
        try:
            tg.send_plain(digest.render_error("Контейнер не запустился", detail))
        except Exception as send_exc:  # noqa: BLE001
            _log(f"не удалось сообщить о сбое старта: {send_exc}")
        return 1

    if args.analyze:
        if not cfg.analysis_enabled:
            _log("разбор выключен: нужны LLM_BASE_URL, LLM_API_KEY и ANALYZE_TOP > 0")
            return 2
        try:
            return _analyze_one(cfg, deps, args.analyze,
                                now=datetime.now().astimezone(),
                                dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            _log(f"разбор не удался: {type(exc).__name__}: {exc}")
            return 1

    if args.once or args.bootstrap:
        try:
            run_once(cfg, deps, now=datetime.now().astimezone(),
                     bootstrap=args.bootstrap)
        except Exception:  # noqa: BLE001 — run_once уже сообщил и залогировал
            return 1
        return 0

    should_stop = install_sigterm_handler()
    _log(f"планировщик запущен, расписание {cfg.schedule!r}")

    startup_now = datetime.now().astimezone()
    _send_startup_message(cfg, deps, now=startup_now)
    _run_startup_catch_up(cfg, deps, now=startup_now)

    def job() -> None:
        run_once(cfg, deps, now=datetime.now().astimezone())

    run_forever(
        job, cfg.schedule,
        should_stop=should_stop,
        on_error=lambda exc: _log(f"прогон упал, ждём следующего срока: {exc}"),
    )
    _log("остановлен по сигналу")
    return 0


if __name__ == "__main__":
    sys.exit(main())
