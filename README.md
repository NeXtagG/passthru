# Passthru

GTK4-приложение для сборки single-GPU passthrough VM. Проверяет всё, что можно,
не запуская виртуалку.

## Установка (любой компьютер)

```bash
git clone https://github.com/NeXtagG/passthru && cd passthru
bash install.sh
```

Скрипт определяет дистрибутив (Arch / Debian/Ubuntu / Fedora / openSUSE) и через
`sudo` ставит QEMU, OVMF, swtpm, libvirt, dnsmasq, PyGObject и libadwaita,
включает libvirt, добавляет тебя в группу `libvirt`, поднимает сеть `default`,
настраивает firewall под интернет в VM, ставит диспетчер хуков и ярлык в меню.
Повторный запуск безопасен. После добавления в группу `libvirt` нужно
перелогиниться.

Пакеты вручную (если менеджер не распознан): `qemu` (полный), `edk2-ovmf`,
`swtpm`, `libvirt`, `dnsmasq`, PyGObject (`python-gobject`/`python3-gi`),
`libadwaita` + GTK4-интроспекция.

## Запуск

```bash
./passthru.sh
```

или «Passthru» в меню приложений.

## Вкладки

Check · GPU · OS · Drive · CPU · RAM · Audio · Network · Peripheral · Preset · Save.
У каждой — значок ✓ / ⚠ / ✗ по её проверкам.

- **RAM** 8 / 12 / 16 / 24 / 32 ГБ или своё. Недоступные для хоста варианты серые.
- **CPU** 2/4 … 16/32 или своё; пиннинг парами гиперпотоков, ядро 0 — хосту.
- **Network** NAT / мост / macvtap / без сети. Блок «Интернет и firewall»
  определяет ufw/firewalld/nftables и чинит форвардинг моста (кнопка под sudo).
- **Save** собирает `domain.xml`, диск, `virsh define`, показывает команды
  установки хуков и настройки сети.

## Маскировка (stealth)

Включается **только** на машине с кастомным QEMU (`/usr/local/bin/qemu-system-x86_64`)
и SMBIOS-блобами хоста в `/var/lib/libvirt/smbios`. На всех остальных компьютерах
приложение работает как обычный конструктор проброса: Hyper-V enlightenments,
без подмены SMBIOS. Это не настраивается в переносимой версии намеренно.

## Проверка без запуска VM

```bash
./passthru.sh --selftest        # схема libvirt + сборка командной строки QEMU + dry-run хуков
./passthru.sh --selftest --tcg  # + реальные таблицы SMBIOS из QEMU в безголовом TCG-госте
```

## Хуки single-GPU

Останавливают дисплей-менеджер перед запуском VM и возвращают после; гасят
hugepages/governor. Ставятся в `/etc/libvirt/hooks`, поэтому только через sudo
(кнопка «Установить (sudo)» на вкладке Save). Если памяти под hugepages не
хватило — запуск отменяется, экран не трогается.

## Файлы VM

`~/.local/share/passthru/vms/<имя>/`: `domain.xml`, `config.json`, `hooks/`,
`install-hooks.sh`, `setup-network.sh`, `smbios/` (только в режиме маскировки).

## Лицензия

GPL-3.0 (`LICENSE`): код можно свободно использовать и изменять, но производные проекты тоже должны
оставаться открытыми под GPL-3.0.
