#!/bin/sh
# Открыть интерфейс «Дирижёра» на весь экран прибора (сенсорный экран DSI или монитор HDMI).
# Экран самого прибора входит без кода (web.trust_localhost: true).
#
# Автозапуск при входе в графический стол — ПРОВЕРИТЬ НА ЖЕЛЕЗЕ, зависит от версии системы:
#   Raspberry Pi OS (Wayland, labwc):  echo "/home/pi/dirizher/system/kiosk.sh &" >> ~/.config/labwc/autostart
#   Raspberry Pi OS (X11, LXDE):       echo "@/home/pi/dirizher/system/kiosk.sh" >> ~/.config/lxsession/LXDE-pi/autostart
#
# Свой адрес:  DIRIZHER_URL=http://localhost:8081 system/kiosk.sh

URL="${DIRIZHER_URL:-http://localhost:8080}"

# ждём, пока служба поднимет интерфейс (до 60 с)
i=0
while [ $i -lt 60 ]; do
  if command -v curl >/dev/null 2>&1; then
    curl -s -o /dev/null "$URL/api/hello" && break
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null "$URL/api/hello" && break
  else
    sleep 5; break
  fi
  i=$((i + 1)); sleep 1
done

for b in chromium-browser chromium google-chrome firefox; do
  if command -v "$b" >/dev/null 2>&1; then BROWSER="$b"; break; fi
done
if [ -z "$BROWSER" ]; then
  echo "браузер не найден: sudo apt install chromium-browser (или chromium)" >&2
  exit 1
fi

if [ "$BROWSER" = "firefox" ]; then
  exec firefox --kiosk "$URL"
fi
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 "$URL"
