# podkop-sub

Imports VLESS links from a subscription into a [podkop](https://github.com/itdoginfo/podkop)
section on OpenWrt, over SSH and `uci`. Terminal menu with arrow keys, works in Termius,
MobaXterm and any Linux terminal. Languages: English and Russian.

- Shows servers with country, city and flag; you choose which ones to write.
- Checks availability: delay through `podkop clash_api`, and new subscription links are
  checked before they are written (TCP, then a full Reality check with sing-box).
- Protected links: the script never deletes, replaces or updates them.
- Scheduled sync (`/etc/cron.d`): adds new links, removes missing ones, replaces dead ones,
  applies with `reload` and verifies the result. Optional Telegram notifications.

Русская версия: [README.ru.md](README.ru.md).

## Install (Debian 12/13 or Ubuntu, as root)

    git clone https://github.com/<you>/podkop-sub.git
    cd podkop-sub
    sh install.sh            # installs into /opt/podkop-sub, command: podkop-sub
    podkop-sub               # first run opens the setup wizard

`install.sh` also tries to download sing-box (for the full Reality check). If that fails,
the check uses TCP only. You can put the binary at `/usr/local/bin/sing-box` yourself.

## Router access

The wizard creates a separate ed25519 key and shows the command to add it on the router:

    echo 'ssh-ed25519 AAAA… podkop-sub' >> /etc/dropbear/authorized_keys
    chmod 600 /etc/dropbear/authorized_keys

The user must be root: on OpenWrt only root can change `/etc/config`. You can restrict the
key with `from="IP-of-this-host"` in `authorized_keys`.

## Menu

- **Update links from subscription**: pick links, choose the action (replace / add / merge),
  review the diff, write, apply.
- **Links on the router**: show, availability check, delete, add your own link, check new
  subscription links without writing, backups and rollback.
- **Podkop**: status, restart, reload, log.
- **Schedule**: status, enable or change, disable, run now (dry or full), log.
- **Settings**: subscriptions, router, section, link types, excluded and preferred countries,
  protected links, sync parameters, Telegram, flag emoji, language, setup wizard.
- **Help**: keys, sync steps, command-line flags and exit codes.

Keys: ↑↓ and Enter; Space marks items in lists; `a` all, `i` invert; Ctrl+C steps back.

## Command line

    podkop-sub --list                 # subscription after filters
    podkop-sub --check                # availability of links on the router
    podkop-sub --check-sub            # new subscription links: TCP / Reality, writes nothing
    podkop-sub --sync --dry-run       # what a scheduled run would do
    podkop-sub --sync                 # the scheduled run itself (cron)
    podkop-sub --update --action merge [--only-preferred] [--restart]
    podkop-sub --rollback

## Several subscriptions

Settings → Subscriptions lists every subscription: add one, rename it, enable or disable it,
show its links, or delete it. Config (`config.toml`) keeps them as `[[subscriptions]]` blocks.

- All subscriptions are written into the same podkop section.
- Filters (link types, excluded and preferred countries) are shared by all of them.
- If one server is in several subscriptions, the first one in the list wins.
- Each link taken from a subscription remembers its owner (in `state.json`). Links added by
  hand have no owner: the script never removes them.
- A subscription that could not be downloaded does not change its own links. Its links stay
  on the router until it is available again. Retries happen only when none of the
  subscriptions could be downloaded.
- Deleting a subscription turns its links into manual ones (the script stops removing them).
- Old single-subscription configs are converted automatically to one subscription named `main`.

## Sync in detail

1. Downloads all enabled subscriptions (retries only if none of them downloaded) and applies the filters.
2. Checks each link on the router through `podkop clash_api get_proxy_latency`.
   Running sing-box nodes are matched by host, port and SNI, not by position.
3. Checks candidates from the subscription before writing them: TCP first, then a full
   Reality check with a temporary sing-box if the binary is installed. Limit per run:
   `sync.probe_max`.
4. Plans: adds new links (preferred countries only when preferences are set, the same as the
   manual menu); removes links missing from the subscription (optional); replaces a dead link
   with a fresh copy that responds. Protected links are never touched. Removal applies only to
   links whose own subscription downloaded successfully and no longer lists them.
5. Nothing to change: exits. The active node is dead and a live one exists: switches without
   restart.
6. Otherwise: backup, write, `podkop reload`, verify that sing-box runs exactly the written
   list (restart as fallback), check availability again.
7. Reports to Telegram if configured.

Exit codes: `0` success or no changes; `1` subscription or router error (list unchanged);
`2` written but not applied; `3` protected list not set up yet.

## Telegram

Settings → Telegram: bot token (from @BotFather) and chat id; there is a test message.
Reports: list changed, active node switched, errors (subscription errors after N consecutive
failures, default 3).

## Configuration and security

`~/.config/podkop-sub/config.toml` is created with mode 600: it holds the subscription URL
(with its token). The router key `id_ed25519` is also private. Neither file should be
published or committed.

## Tests

    python3 -m unittest discover -s tests -t .

Tests use fake `ssh`, `uci`, `podkop`, `/etc/init.d/podkop` and sing-box (tests/fakebin) and a
questionary stub. They do not replace a real router: before enabling the schedule, compare
`podkop-sub --check` with N/A in LuCI and run `podkop-sub --sync --dry-run`.

## License

MIT, see [LICENSE](LICENSE).
