# Интеграции «Дирижёра» 0.3.0 — 8 внешних программ

Пакет добавляет 6 новых «лезвий», проверку структуры прошивки через binwalk и замер логическим
анализатором через sigrok-cli. Всё вызывается отдельными процессами (PRINCIPLES.md, п. 11),
каждое лезвие выключено, пока вы не добавите его секцию в `config.yaml` с `enabled: true`.

**Ничего из этого не проверено на железе.** Форматы вывода взяты из документации и типичных
журналов; весь цикл проверен 54 тестами на подставных программах и эмуляторе Bus Pirate 5.

Версия 0.3.0, потому что 0.2.0 — это «Интерфейсы» (Штурман, браузер, мастер, конструктор).
Пакет собран так, чтобы **не затирать** их: `__main__.py`, `config.yaml`, `interface/`, основные
udev-правила и MANUAL не меняются. Изменены только три файла ядра (раздел 5).

## 1. После обновления (на Raspberry Pi)

```bash
cd ~/dirizher && git pull
python3 -m unittest -v                     # 96 тестов, OK
python3 -m dirizher plugins                # новые лезвия видны как «выключен»
sudo cp system/99-dirizher-tools.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules
sudo usermod -aG plugdev,dialout $USER     # затем перелогиниться
```
Чтобы включить лезвие, скопируйте его секцию из `docs/config-snippet-0.3.0.yaml` в `config.yaml` и поставьте `enabled: true`.

## 2. В каком порядке включать

Сначала то, что ничего не пишет, потом лезвия — по одному, по плану STATUS.md.

| № | Что | Риск | Почему здесь |
|---|---|---|---|
| 1 | **binwalk** — проверка `binwalk_expect` | нет (только читает дамп) | Сразу полезно для SPI-дампов с flashrom |
| 2 | **sigrok-cli** — замер | нет (только приём сигналов) | «Замер» перед чтением: читает ли процессор флеш |
| 3 | **Bus Pirate 5** — `bp5_i2c` | низкий (256 Б – 64 КБ, WP) | Этап 2 из STATUS.md |
| 4 | **probe-rs** — `probe_rs` | средний (МК) | Этап 3 из STATUS.md |
| 5 | **minipro** — `minipro` | низкий/средний | Этап 3 из STATUS.md |
| 6 | **OpenOCD** — `openocd` | средний | Запасной путь, если probe-rs не знает чип/адаптер |
| 7 | **avrdude** — `avrdude` | средний (chip erase) | AVR-платы |
| 8 | **esptool** — `esptool` | средний (NVS, шифрование) | ESP-платы |

Для каждого лезвия первый раз: `detect` → `run --dry-run` → исправное устройство как эталон →
только потом ремонт. Каждое расхождение с этой инструкцией — сначала тест, потом исправление (MANUAL 2.6).

## 3. Сводка

| Программа | Файл | Класс цели | Лицензия | Установка |
|---|---|---|---|---|
| probe-rs | `plugins/probe_rs.py` | `mcu.arm` | MIT/Apache-2.0 | официальный установщик с github.com/probe-rs/probe-rs (`probe-rs-tools-installer.sh`) или `cargo install probe-rs-tools --locked` |
| OpenOCD | `plugins/openocd.py` | `mcu.arm` | GPL-2.0 | `sudo apt install openocd` |
| minipro | `plugins/minipro.py` | по имени чипа | GPL-3.0 | сборка из gitlab.com/DavidGriffith/minipro (нужны libusb-1.0-0-dev, pkg-config) |
| Bus Pirate 5 | `plugins/bp5_i2c.py` + `helpers/bp5_i2c.py` | `memory.i2c_eeprom` | — (свой код, stdlib) | ничего; пользователь в группе `dialout` |
| avrdude | `plugins/avrdude.py` | `mcu.avr` | GPL-2.0 | `sudo apt install avrdude` |
| esptool | `plugins/esptool_flash.py` | `mcu.esp` | GPL-2.0 | `pipx install esptool` (на Raspberry Pi OS системный pip закрыт) |
| binwalk | `core/checks.py` → `binwalk`, `binwalk_expect` | любой | MIT | `sudo apt install binwalk` (2.x) или 3.x через cargo |
| sigrok-cli | `extras/sigrok.py` (`python3 -m dirizher.extras.sigrok`) | — | GPL-3.0 | `sudo apt install sigrok-cli` |
| Modbus RTU (0.4.0, навык) | `skills/modbus_rtu/skill.py` + `rtu.py` | `device.modbus` | — (свой код, stdlib) | ничего; переходник USB–RS-485, пользователь в группе `dialout` |

Общие ключи любого лезвия в `config.yaml` (образец — `docs/config-snippet-0.3.0.yaml`): `enabled`, `programmer_voltage` (своё напряжение
адаптера), `extra_args` (доп. параметры; опасные отклоняются), `probe_timeout`/`read_timeout`/`write_timeout`.

## 4. Карточки

### 4.1. probe-rs — ARM Cortex-M / RISC-V по SWD/JTAG
- **Даёт:** чтение и запись флеша МК через ST-Link, CMSIS-DAP, J-Link, Pico-probe.
- **Настройки:** `chip` (имя из `probe-rs chip list`), `flash_base`, `flash_size`, `protocol`, при нескольких отладчиках — `probe`.
- **Команды:** probe — `probe-rs list` + `probe-rs info`; read — `probe-rs read --chip … b8 <адрес> <байт>`;
  write — `probe-rs download --chip … --binary-format bin --base-address <адрес> файл`
  (у старых версий `--format`: определяется по `download --help`).
- **Опознание:** модель задаёт человек, связь с ядром проверяется → уверенность 0.75.
- **Запрещено:** `--allow-erase-all`, `--chip-erase`, `erase`. Защиту RDP не снимаем — объясняем причину.
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** формат вывода `probe-rs read b8` (разбор терпимый, при несовпадении числа
  байт — понятная ошибка, запись не начнётся); строки `probe-rs info`; время чтения 64–512 КБ.

### 4.2. OpenOCD — то же через конфиги OpenOCD
- **Даёт:** редкие чипы и адаптеры (FT2232, GPIO Raspberry Pi), которых нет в probe-rs.
- **Настройки:** `interface`, `target`, `mcu`, `flash_base`, `flash_size`; `pre_init` (например, `adapter speed 1000`); `halt_cmd`.
- **Команды:** probe — `-c init -c shutdown` (IDCODE/DPIDR, «processor detected», «Target voltage»);
  read — `-c init -c "reset halt" -c "dump_image {файл} адрес размер" -c shutdown`;
  write — `-c "program {файл} verify адрес" -c shutdown`.
- **Запрещено:** команды со словами `unlock`, `lock`, `mass_erase`, `protect`, `option(s)_write/load`, `erase_address`.
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** нужен ли `reset halt` для вашей платы; строки опознания в вашей версии.
- Не включайте `probe_rs` и `openocd` одновременно на один МК: оба ответят, и система спросит, какой выбрать.

### 4.3. minipro — T48 / TL866II+
- **Даёт:** память в панельке: 24Cxx, 25xx, 93Cxx, параллельные 27/28/29/39.
- **Настройки:** `device` — точное имя из `minipro -L <часть имени>`; класс определяется сам
  (24C… → I2C EEPROM, W25/MX25/GD25… → SPI NOR с `jedec_id` — подходят уже готовые профили), можно задать `target_class`.
- **Команды:** probe — `minipro -p <device> -D` (только ID); read — `-r файл`; write — `-w файл` (minipro сам стирает и сверяет).
- **Запрещено:** `-y` (неверный ID), `-s/-S` (неверный размер), `-v` (без сверки), `-E`, `-c config`, `--fuses`, `--lock`, `-x`.
  Микроконтроллеры через minipro не обслуживаются (стирание заденет EEPROM и фьюзы).
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** строку `Chip ID: 0x… OK` и текст при несовпадении; ответ на `-D` для чипов без ID.

### 4.4. Bus Pirate 5 — I2C EEPROM 24Cxx
- **Даёт:** 24C01…24C512 на плате или в прищепке; BP5 сам питает чип (`psu_voltage`) и включает подтяжки.
- **Как устроено:** плагин запускает помощник `helpers/bp5_i2c.py` отдельным процессом; тот говорит
  с терминалом BP5 (`m` → I2C, `W 3.3`, `P`, `[0xA0 0x00]`, `[0xA1 r:16]`), разбирает `TX:/RX:`.
  Чтение по 16 байт, запись — только изменившиеся куски по 8 байт, выровненные по 8 (граница страницы не пересекается).
- **Настройки:** `port`, `eeprom`, `address`, `psu_voltage` (0 — питание от платы, тогда задайте `programmer_voltage`), `pullups`.
- **Опознание:** у 24Cxx нет ID — модель из настроек, ответ ACK по адресу → уверенность 0.7.
- **Безопасность:** WP (вывод 7) на питании → запись не пройдёт сверку, система предложит вернуть копию «до».
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** номер пункта I2C в меню `m` (ищется по тексту), вопросы после `W 3.3`,
  раскраску VT100, перенос длинной строки RX, время чтения 24C256.

### 4.5. avrdude — AVR (ATmega/ATtiny)
- **Даёт:** USBasp, AVRISP, Arduino-as-ISP, SerialUPDI.
- **Главное решение:** образ = **flash + EEPROM**. При записи flash avrdude делает chip erase, который
  стирает и EEPROM. Поэтому EEPROM входит в копию «до» и сверку, пишется той же командой, а в профиле
  ATmega328P помечена `preserve` — настройки устройства берутся с текущего чипа.
- **Настройки:** `programmer`, `part`, `port`, `bitclock`, `programmer_voltage` (USBasp обычно 5 В), `memories`, `flash_size`, `eeprom_size`.
- **Команды:** probe — `avrdude … -n` (подпись); read/write — `-U flash:r|w:файл:r -U eeprom:r|w:файл:r`.
  Хвост flash из 0xFF avrdude не сохраняет — плагин дополняет до `flash_size`.
- **Запрещено:** `-u` (safemode фьюзов), `-F`, `-D`, `-e`, `-t`, любые `-U` в `extra_args`. Фьюзы и lock-биты не читаются в образ и не пишутся.
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** строку подписи (6.x `0x1e950f`, 7–8.x `1E 95 0F`), срезание хвоста 0xFF в вашей версии.

### 4.6. esptool — ESP8266 / ESP32
- **Даёт:** полный дамп SPI-флеша модуля по USB-UART; v4 и v5 (дефисы) определяются автоматически.
- **Настройки:** `port`, `baud`, `chip`, `cmd`.
- **Команды:** probe — `flash_id` (чип, MAC, ID и размер флеша); read — `read_flash 0 <размер>`;
  write — `write_flash --flash_mode keep --flash_freq keep --flash_size keep 0x0 файл`
  (`keep` — иначе esptool поправит заголовок загрузчика, и сверка не совпадёт; это проверено тестом).
- **MAC в отпечатке:** откат — только на ту же плату.
- **Запрещено:** `--encrypt`, `--force`, игнорирование настроек шифрования, `erase_flash`/`erase_region`.
- **Важно:** при включённом шифровании флеша дамп — шифротекст: эталон с другой платы не поможет.
  NVS (Wi-Fi, ключи, калибровка) — уникальные данные: добавьте `preserve` в профиль платы по её таблице разделов.
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** строки `Chip is` / `Chip type:`, `MAC:`, `Detected flash size` в вашей версии.

### 4.7. binwalk — проверка структуры
- Уже было: проверка `binwalk` показывает, что лежит в дампе.
- **Новое:** `binwalk_expect` — fail, если не найдена ожидаемая сигнатура, и предлагает восстановление:
  ```yaml
  checks:
    - type: binwalk_expect
      expect: ["uImage header", "Squashfs filesystem"]
  ```
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** точные названия сигнатур берите из вывода binwalk на **исправном** дампе этой платы.

### 4.8. sigrok-cli — замер логическим анализатором
- `python3 -m dirizher.extras.sigrok --scan` — найти анализатор.
- `python3 -m dirizher.extras.sigrok --channels D0,D1,D2,D3 --samplerate 1m --time 2000 --decode "spi:clk=D0:mosi=D1:miso=D2:cs=D3" --annotation spi`
- Запись `.sr` и расшифровка `.txt` — в `data/captures/`, сеанс вида `capture` — в истории и журнале.
- **Безопасность:** только приём сигналов. Входы дешёвых анализаторов — до 5 В.
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** драйвер вашего анализатора и синтаксис декодера (`sigrok-cli -P spi --show`).

### 4.9. Modbus RTU — приборы по RS-485 (навык, 0.4.0)
- **Даёт:** чтение и смену настроек термопреобразователей, регуляторов, счётчиков: «Настройка параметров»
  по карте профиля, копия «до/после», сверка, откат, эталон настроек.
- **Как устроено:** навык `skills/modbus_rtu`, помощник-процесс `rtu.py` (stdlib, termios): функции 3/4 — чтение,
  6 (или 16 при `write_function: 16`) — запись, CRC, повторы, `--echo` для переходников с эхом.
  Прибор = окно регистров настроек (`bus: start, count, function` в профиле); пишутся только изменившиеся регистры.
- **Настройки** (`skill.yaml` → `settings` или `config.yaml` → `plugins: modbus_rtu`): `enabled`, `port`, `baud`,
  `parity`, `stopbits`, `unit`, `model`, `timeout`, `retries`, `write_function`, `echo`.
- **Опознание:** чтение одного регистра; модель задаёт человек (`model`) → уверенность 0.7. Напряжение логики
  не сверяется (`voltage_check = False`): прибор питается сам, шина развязана — вместо вопроса напоминание о проводке.
- **Защита:** значения вне min/max, не по шагу, «только чтение» → запись не начнётся; живые регистры в окне →
  повторные чтения не совпадут → запись заблокирована; прибор не принял значение → сверка не совпала →
  предложен возврат копии «до».
- **ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:** эхо переходника, паузы между кадрами на вашей скорости, поддержку функции 16,
  карту регистров своего прибора (учебная `thermo_example.yaml` — адреса придуманы).
- **Тесты:** `tests/fakes/modbus_emulator.py` (прибор на псевдотерминале: 3/4/6/16, исключения, «молчит»,
  «живой» и «упрямый» регистры), `tests/test_params.py`.

## 5. Изменение ядра (одно) и почему

**Напряжение адаптера у каждого плагина.** Было одно `safety.programmer_voltage` на всё. Но USBasp — 5 В,
CH341A — 3.3 В, Bus Pirate 5 — регулируемый. С одним значением система либо блокировала бы
правильную работу, либо задавала бы неверный вопрос. Теперь `plugins: <имя>: programmer_voltage`
перекрывает общее значение; без него всё как раньше.
Файлы: `core/safety.py` (`voltage_check(profile, programmer_voltage=None)`),
`core/pipeline.py` (`adapter_voltage()`, `_voltage_gate(profile, plugin)`). Тесты: OpenOCD на 5 В блокируется
перед чтением; avrdude спрашивает про 5.0 В; BP5 — про своё `psu_voltage`.

Остальное — без изменения ядра: новая проверка `binwalk_expect` добавлена в штатную точку расширения
(`core/checks.py`, MANUAL 2.3). Замер sigrok — отдельный модуль со своей точкой входа: командную строку
(`__main__.py`) и интерфейсы он не меняет, тексты исходов замера регистрирует сам.

Штурман (0.3.1): раздел «Подключить инструмент», уроки по каждому лезвию и действие «замер» — без
правки config.yaml с телефона: включение лезвия — по-прежнему в config.yaml на Pi.

## 6. Тесты без железа

- `tests/fakes/fake_tools.py` — подставные probe-rs, openocd, minipro, avrdude, esptool(.py), binwalk, sigrok-cli.
  Тест кладёт в PATH обёртки с настоящими именами; каждый вызов пишется в журнал, и тесты проверяют,
  что опознание только читает, а опасных параметров не было.
- `tests/fakes/bp5_emulator.py` — терминал Bus Pirate 5 с 24C02 на псевдотерминале (страницы, WP, NACK).
- `tests/test_integrations.py` (45) и `tests/test_bp5.py` (9). Проверены «мутацией»: если avrdude пишет только flash,
  esptool не передаёт `keep` или BP5 пишет по 16 байт — тесты падают.
