"""Точка входа: сборка зависимостей, один прогон, режим демона."""
from __future__ import annotations

import argparse
import html
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from crashdigest import digest
from crashdigest.auth import TokenProvider
from crashdigest.config import Config, ConfigError, load
from crashdigest.crashlytics import CrashlyticsClient, CrashlyticsError
from crashdigest.digest import render_weekly, week_totals, weekly_growth
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


def build_deps(cfg: Config, *, telegram: object | None = None) -> Deps:
    """`telegram`, если передан, используется как есть — так main() может
    собрать отправителя раньше State/CrashlyticsClient и сообщить об их
    отказе тем же каналом, а не голым traceback.
    """
    tokens = TokenProvider(cfg.refresh_token, cfg.client_id, cfg.client_secret)
    return Deps(
        crashlytics=CrashlyticsClient(cfg.project, cfg.app_id, tokens),
        state=State(cfg.state_path),
        telegram=telegram if telegram is not None else Telegram(
            cfg.bot_token, cfg.chat_id, proxy=cfg.proxy
        ),
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
        deps.telegram.send(digest.render_bootstrap(len(rows), cfg.app_name))
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
        truncated=truncated, scanned=len(rows),
        versions=list(versions.display_versions),
    )

    deps.telegram.send(messages)          # сначала доставка…
    deps.state.record(rows, now)
    deps.state.mark_success(now)          # …и только потом сдвиг окна
    _log(f"отправлено: новых {len(new_rows)} из {len(rows)} в окне")

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


def _notify_failure(deps: Deps, headline: str, detail: str) -> None:
    """Пытается сообщить о поломке в тот же канал. Молчание — худший исход."""
    try:
        deps.telegram.send(digest.render_error(headline, detail))
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
    return (
        f"🚀 <b>{html.escape(cfg.app_name)}</b>: контейнер запущен\n"
        f"зона: {html.escape(zone)} · расписание: <code>{html.escape(cfg.schedule)}</code>\n"
        f"следующий прогон: {next_text}\n"
        f"последний успешный прогон: {last_text}"
    )


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
            tg.send(digest.render_error("Контейнер не запустился", detail))
        except Exception as send_exc:  # noqa: BLE001
            _log(f"не удалось сообщить о сбое старта: {send_exc}")
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
