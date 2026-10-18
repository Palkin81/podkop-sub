#!/bin/sh
# Installs podkop-sub into /opt/podkop-sub and creates the /usr/local/bin/podkop-sub command.
# Tested on Debian 12/13 and Ubuntu. Run as root.
set -e
DIR="${1:-/opt/podkop-sub}"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (apt and /usr/local/bin need it)." >&2
    exit 1
fi

apt-get update
apt-get install -y python3 python3-venv openssh-client cron curl

mkdir -p "$DIR"
rm -rf "$DIR/podkop_sub"
cp -r "$SRC/podkop_sub" "$SRC/requirements.txt" "$DIR"/
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip
"$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

cat > /usr/local/bin/podkop-sub <<WRAP
#!/bin/sh
exec env PYTHONPATH="$DIR" "$DIR/venv/bin/python" -m podkop_sub "\$@"
WRAP
chmod +x /usr/local/bin/podkop-sub

systemctl enable --now cron 2>/dev/null || true

# sing-box enables the full (Reality) check of subscription links before they are written.
# It is optional: without it only the TCP check is done. Download is attempted from GitHub.
if [ ! -x /usr/local/bin/sing-box ]; then
    case "$(dpkg --print-architecture)" in
        amd64) SB_ARCH=amd64 ;;
        arm64) SB_ARCH=arm64 ;;
        *) SB_ARCH="" ;;
    esac
    if [ -n "$SB_ARCH" ]; then
        # приватная временная папка: имена в общем /tmp предсказуемы, а скрипт работает от root
        WORK=$(mktemp -d)
        trap 'rm -rf "$WORK"' EXIT
        TAG=$(curl -fsS https://api.github.com/repos/SagerNet/sing-box/releases/latest 2>/dev/null \
              | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1)
        ASSET="sing-box-$TAG-linux-$SB_ARCH.tar.gz"
        installed=""
        if [ -n "$TAG" ]; then
            # SHA-256 берём из метаданных релиза и сверяем с архивом до установки
            DIGEST=$(curl -fsS "https://api.github.com/repos/SagerNet/sing-box/releases/tags/v$TAG" 2>/dev/null \
                     | python3 -c "import json,sys; d=json.load(sys.stdin); print(next((a.get('digest','').split(':')[-1] for a in d.get('assets',[]) if a.get('name')=='$ASSET'),''))" 2>/dev/null)
            if [ -n "$DIGEST" ] && curl -fsSL -o "$WORK/sb.tgz" \
                "https://github.com/SagerNet/sing-box/releases/download/v$TAG/$ASSET"; then
                ACTUAL=$(sha256sum "$WORK/sb.tgz" | cut -d' ' -f1)
                if [ "$ACTUAL" = "$DIGEST" ]; then
                    tar -xzf "$WORK/sb.tgz" -C "$WORK"
                    install -m 755 "$WORK/sing-box-$TAG-linux-$SB_ARCH/sing-box" /usr/local/bin/sing-box
                    installed="$TAG"
                else
                    echo "sing-box checksum mismatch, not installed." >&2
                fi
            fi
        fi
        if [ -n "$installed" ]; then
            echo "sing-box $installed installed to /usr/local/bin/sing-box"
        else
            echo "sing-box not installed (download or checksum check failed): links will be checked by TCP only (see README)." >&2
        fi
    fi
fi

echo "Done. Run: podkop-sub"
