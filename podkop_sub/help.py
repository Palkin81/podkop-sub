"""Справка на двух языках (показывается из меню «Help» / «Справка»)."""
from __future__ import annotations

from .i18n import get_language

HELP = {
    "en": """\
podkop-sub — imports VLESS links from a subscription into a podkop section on OpenWrt.

MENU
  Update links from subscription  pick links, choose what to do, review, write.
  Links on the router             show, availability check, delete, add, backups,
                                  check new subscription links (no changes).
  Podkop                          status, restart, reload, log.
  Schedule                        automatic runs via /etc/cron.d/podkop-sub.
  Settings                        subscriptions, router, filters, protected links,
                                  sync parameters, Telegram, language.
  Settings → Subscriptions        add, rename, enable/disable, delete, show links.

KEYS
  ↑ ↓ Enter   move and choose          Space   mark in lists
  a / i       all / invert             Ctrl+C  back one step at a prompt

AVAILABILITY
  Shows the delay in ms (via podkop clash_api), N/A = no response, ? = test failed.
  New subscription links are checked before they are written: TCP first, then a
  full Reality check if sing-box is installed (sync → probe setting).

PROTECTED LINKS
  The script never deletes, replaces or updates them, even if they do not respond.

SYNC (what a scheduled run does)
  1. download all enabled subscriptions  2. check router links and candidates
  3. add new (preferred countries only, like the manual menu)
  4. remove links that their own subscription no longer lists (optional);
     manual links are never removed, links of a subscription that failed are kept
  5. replace dead links with fresh copies that respond
  6. write → reload → verify → restart if needed  7. Telegram report

COMMAND LINE
  --list  --check  --check-sub  --sync [--dry-run]  --update [--action …]
  --rollback  --wizard  --config PATH

EXIT CODES (--sync)
  0 ok or no changes · 1 subscription or router error · 2 written but not applied
  3 protected list not set up yet
""",
    "ru": """\
podkop-sub — импортирует VLESS-ссылки из подписки в секцию podkop на OpenWrt.

МЕНЮ
  Обновить ссылки из подписки   выбрать ссылки, действие, проверить, записать.
  Ссылки на роутере             показать, проверка доступности, удалить, добавить,
                                бэкапы, проверка новых ссылок из подписки (без записи).
  Podkop                        статус, перезапуск, reload, лог.
  Расписание                    автозапуск через /etc/cron.d/podkop-sub.
  Настройки                     подписки, роутер, фильтры, защищённые ссылки,
                                параметры синхронизации, Telegram, язык.

КЛАВИШИ
  ↑ ↓ Enter   перемещение и выбор      пробел   отметить в списке
  a / i       все / инверсия           Ctrl+C   шаг назад в вопросе

ДОСТУПНОСТЬ
  Показывает задержку в мс (через podkop clash_api), N/A — не отвечает, ? — тест не удался.
  Новые ссылки из подписки проверяются до записи: сначала TCP, затем полная проверка
  Reality, если установлен sing-box (Настройки → Параметры синхронизации → проверка).

ЗАЩИЩЁННЫЕ ССЫЛКИ
  Скрипт никогда их не удаляет, не заменяет и не обновляет, даже если они не отвечают.

СИНХРОНИЗАЦИЯ (что делает запуск по расписанию)
  1. скачивает все включённые подписки  2. проверяет ссылки роутера и кандидатов
  3. добавляет новые (только страны-предпочтения, как в ручном меню)
  4. удаляет ссылки, которых больше нет в их подписке (можно выключить);
     ручные ссылки не трогает, ссылки недоступной подписки оставляет
  5. заменяет мёртвые ссылки свежими, которые отвечают
  6. запись → reload → проверка → при необходимости restart  7. отчёт в Telegram

КОМАНДНАЯ СТРОКА
  --list  --check  --check-sub  --sync [--dry-run]  --update [--action …]
  --rollback  --wizard  --config ПУТЬ

КОДЫ ВОЗВРАТА (--sync)
  0 успех или изменений нет · 1 ошибка подписки или роутера · 2 записано, но не применено
  3 защищённый список ещё не настроен
""",
}


def help_text() -> str:
    return HELP.get(get_language(), HELP["en"])
