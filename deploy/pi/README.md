# Плата как постоянный резидентный прокси

То же, что делает телефон (SSH-клиент поднимает `-R` туннель на VPS, сервер ходит в F1 через
`socks5h://172.18.0.1:1081`), только без человека: плата в домашнем Wi-Fi держит туннель сама,
следит за ним и чинит, если что-то упало.

```
 F1 container ──socks5h://172.18.0.1:1082──▶ socat (f1-proxy-relay) ──▶ 127.0.0.1:1082 на VPS
                                                                            │  ssh -R
                                                                            ▼
                                               плата: microsocks 127.0.0.1:1082 ──▶ домашний IP
```

Порты источников: `1080` — ноутбук (`make tunnel`), `1081` — телефон, `1082` — плата. Писалось
под Orange Pi Zero 3 (2 ГБ) на Armbian, но подойдёт любая Debian-плата (Raspberry Pi OS тоже).

## Что работает на плате

| Юнит | Что делает |
|---|---|
| `f1-socks.service` | `microsocks` на `127.0.0.1:1082`, наружу в LAN не слушает |
| `f1-tunnel.service` | `ssh -N -R 127.0.0.1:1082:…` на VPS; при любом обрыве ssh выходит (keepalive 15 с × 3), systemd поднимает его снова через 10 с, без лимита попыток |
| `f1-tunnel-watchdog.timer` | раз в 2 минуты — проверки ниже |
| аппаратный watchdog | если зависнет ядро или systemd, плата перезагрузится сама |

Проверки watchdog, по порядку:

1. **Сеть** — пингуется ли роутер (или открывается ли интернет напрямую). 2 провала подряд
   (~4 мин) — Wi-Fi выкл/вкл, 4 — перезапуск сети, 8 (~16 мин) — перезагрузка платы.
2. **Интернет** — есть LAN, но нет интернета (авария у провайдера) — ничего не трогает.
3. **Локальный SOCKS** — не работает при живом интернете — рестарт `f1-socks`.
4. **Сквозная проверка** — заходит по SSH на VPS и просит его скачать `api.ipify.org` через
   `127.0.0.1:1082`, то есть ровно тем путём, каким ходит lap-vision-f1. 2 провала подряд —
   рестарт `f1-tunnel`. Если лежит VPS, плату это не перезагружает.

Ещё `install.sh` выключает энергосбережение Wi-Fi (с ним соединение на этих платах отваливается)
(а если стоит NetworkManager — снимает его лимит в 4 попытки переподключения).

## 1. Записать карту (на Mac)

1. Скачать Armbian для Orange Pi Zero 3: <https://www.armbian.com/orange-pi-zero-3/> → *Direct
   download*, вариант **Minimal (CLI)** (Debian или Ubuntu — без разницы, Xfce не нужен). Плата
   поддерживается сообществом, стабильных сборок нет — только *Rolling Release*; стоит
   `Armbian_community_26.11.0-trunk.108_Orangepizero3_resolute_current_6.18.57_minimal.img.xz`.
2. **Не на рабочем Mac:** Intune-профиль (`mount-controls` → `harddisk-external` → `eject`) сразу
   отключает любой внешний диск, карту там не видит ни Etcher, ни `diskutil`. Нужен другой
   компьютер или Android + USB-картридер + EtchDroid.
3. Распаковать образ: `xz -dk <файл>.img.xz`. Сжатый `.img.xz` balenaEtcher не берёт («Something
   went wrong… check that the archive is not corrupted»), хотя `xz -t` он проходит.
4. Записать `.img` — надёжнее всего `dd` (`diskutil list external` → карта ~32 ГБ, номер `N`;
   чужой номер сотрёт не тот диск):
   ```bash
   diskutil unmountDisk /dev/diskN
   sudo dd if=<файл>.img of=/dev/rdiskN bs=4m status=progress && sync
   diskutil eject /dev/diskN
   ```
   Или Armbian Imager (сам скачивает и пишет). На «диск не читается — инициализировать?» —
   *Игнорировать*: это ext4.

## 2. Первый запуск

Плата без экрана, поэтому в первый раз проще всего через кабель:

1. Вставить карту, подключить плату **Ethernet-кабелем к роутеру**, питание USB-C 5 В / 2 А+.
   Загрузилась — мигает красный светодиод, на разъёме Ethernet горит зелёный. Первая загрузка
   идёт 3–5 минут (расширение ФС на всю карту).
2. Найти её IP. Дома две сети: главный роутер `192.168.70.x` (в него воткнута плата) и Wi-Fi 5 ГГц
   за вторым роутером `192.168.1.x` (на нём Mac); MikroTik-мост адреса плате не выдал. Из
   `192.168.1.x` сеть `192.168.70.x` видна через NAT, так что ищем открытый SSH там (было
   `192.168.70.18`):
   ```bash
   for i in $(seq 2 254); do (nc -z -G 2 192.168.70.$i 22 2>/dev/null && echo 192.168.70.$i) & done; wait
   ```
   Шлюз за Wi-Fi роутером, если сети поменяются: `traceroute -n -m 3 1.1.1.1`.
3. `ssh root@<IP>`, пароль `1234`. Armbian попросит сменить пароль root и создать пользователя.
4. Свой ключ: `ssh-copy-id -i ~/.ssh/id_ed25519.pub root@<IP>` (и `ubuntu@<IP>` для пользователя).

Без кабеля: micro-HDMI + USB-клавиатура, дальше те же шаги в консоли.

## 3–4. Wi-Fi и туннель

В Armbian minimal нет NetworkManager (`nmtui`/`nmcli`): сетью управляют netplan и
systemd-networkd, Wi-Fi прописывает сам `install.sh`.

С Mac, из корня репозитория:

```bash
scp -r deploy/pi root@<IP>:/root/f1-pi
ssh root@<IP>
```

Уже на плате (пароль от Wi-Fi вводится здесь, чтобы не остался в истории на ноутбуке):

```bash
WIFI_SSID='Моя сеть' WIFI_PASSWORD='пароль' SOCKS_PORT=1082 bash /root/f1-pi/install.sh
ip -4 addr show wlan0     # должен появиться IP
```

Wi-Fi 5 ГГц плата тоже видит, но 2.4 ГГц обычно стабильнее через стены. Кабель можно не вынимать:
пока он есть, трафик идёт по нему, Wi-Fi остаётся запасным. Имя `orangepizero3.local` (avahi)
срабатывает через раз — Wi-Fi роутер теряет mDNS; надёжнее алиасы в `~/.ssh/config` на Mac:
`ssh opi` (`192.168.70.18`) и `ssh opi-remote` (через VPS, см. ниже).

Скрипт напечатает в конце строку для `authorized_keys` — она нужна в следующем шаге. Повторный
запуск безопасен (ключ не перегенерируется); перезапускать после смены настроек или сети.

Переменные (все необязательные): `TUNNEL_HOST` (`95.179.154.60`), `TUNNEL_USER` (`ubuntu`),
`TUNNEL_PORT` (`22`), `SOCKS_PORT` (`1082`), `ADMIN_PORT` (`2222`, пусто — выключить),
`WIFI_IFACE` (`wlan0`).

## 5. VPS

1. Добавить напечатанную строку в `~ubuntu/.ssh/authorized_keys`. Она вида
   ```
   restrict,port-forwarding,permitlisten="127.0.0.1:1082",permitlisten="127.0.0.1:2222",command="curl … 127.0.0.1:1082 …" ssh-ed25519 AAAA… f1-tunnel@orangepizero3
   ```
   Ключ платы может только открыть эти два порта на loopback VPS и выполнить проверку: если плату
   украдут, шелла на сервере с ним не получить.
2. Чтобы sshd сам быстро замечал мёртвые туннели (иначе после обрыва Wi-Fi старая сессия держит
   порт до двух часов, и новый туннель не встаёт):
   ```bash
   printf 'ClientAliveInterval 15\nClientAliveCountMax 3\n' | sudo tee /etc/ssh/sshd_config.d/20-f1-tunnel-keepalive.conf
   sudo sshd -t && sudo systemctl reload ssh
   ```
   Это поможет и туннелю телефона. `10-lapvision.conf` рядом ведёт lv-server — его не трогать.
3. `1082` должен быть в GitHub-переменной `F1_PROXY_TUNNEL_PORTS` (сейчас `1080,1081,1082,1083` —
   уже есть). Если нет — добавить и запустить деплой f1 вручную: `deploy_f1.sh` перегенерирует
   `f1-proxy-relay`, а тот в течение минуты зарегистрирует `socks5h://172.18.0.1:1082`, как только
   на порту появится туннель.

## 6. Проверить

Вытащить Ethernet-кабель, перезагрузить плату (`reboot`), подождать пару минут.

На VPS — должен вернуться домашний IP:

```bash
curl --socks5-hostname 127.0.0.1:1082 https://api.ipify.org
```

На плате:

```bash
systemctl status f1-socks f1-tunnel
journalctl -u f1-tunnel-watchdog -n 20     # "ok: VPS exits via <домашний IP>"
journalctl -fu f1-tunnel                   # живой лог туннеля
```

## Мониторинг со стороны сервиса

lap-vision-f1 раз в 2 минуты запрашивает через каждый зарегистрированный прокси
`livetiming.formula1.com/static/StreamingStatus.json` (с адреса VPS напрямую — 403) и отдаёт итог в
`GET /v1/admin/proxy/status` (заголовок `X-Internal-Token`):

```json
{"ok": true, "checked_at": "…", "probe_url": "…",
 "proxies": [{"url": "socks5h://172.18.0.1:1082", "ok": true, "status_code": 200, "latency_ms": 1222,
              "error": null, "checked_at": "…", "last_ok_at": "…"}]}
```

`ok` — F1 доступен хотя бы через один прокси. Смены состояния пишутся в лог контейнера
(`proxy-health: … reaches F1` / `… does not reach F1: …`).

## Доступ к плате из любого места

Плата пробрасывает свой sshd на `127.0.0.1:2222` VPS, поэтому достучаться до неё можно и не из
дома:

```bash
ssh -J ubuntu@95.179.154.60 -p 2222 <пользователь>@127.0.0.1
```

## Всё заново / смена VPS

- **Новая карта или переустановка.** Шаги 1–6; ключ платы будет новым — на VPS в
  `~ubuntu/.ssh/authorized_keys` заменить строку `… f1-tunnel@orangepizero3` на новую из вывода
  `install.sh` (старую удалить). На Mac: `ssh-keygen -R 192.168.70.18` (у платы новый host key) и,
  если IP другой, поправить `HostName`/`HostKeyAlias` у `opi`/`opi-remote` в `~/.ssh/config`.
- **У VPS сменился IP.** Плата сама не узнает — из дома (`opi-remote` в этот момент не работает):
  `ssh opi`, затем `TUNNEL_HOST=<новый IP> SOCKS_PORT=1082 bash /root/f1-pi/install.sh` (Wi-Fi и ключ
  сохранятся). На Mac — `ProxyJump` у `opi-remote`.
- **Новый сервер.** Плюс шаг 5 на нём целиком; relay и порт поднимет ручной деплой f1.
