# SSH-доступ к VPS

> Параметры и пароль — в `.env` (он в `.gitignore`). Здесь только как подключаться.

## Сервер (актуальный, после миграции 01.07.2026)
- IP: `72.56.8.174`, порт `22`, пользователь `root` (хост `msk-1-vm-yek3`)
- ОС: **Ubuntu 22.04**, `python3` в системе — см. `SERVER_PYTHON` в `.env`
- Host key (ssh-ed25519): `SHA256:KqYZBEnG/cqrBqZSWmJzlS2yo2I4K6+uzCtcX6dKQhQ`

Старый сервер `64.188.59.109` (до миграции) выведен из эксплуатации, DNS
(`pay.sushi-house-39.ru`) уже указывает на новый IP.

## На новом сервере работает обычный Windows-OpenSSH и `plink`

В отличие от старого сервера (там штатный `ssh.exe` падал на согласовании KEX
`sntrup761x25519-sha512@openssh.com`), к `72.56.8.174` **обычный `ssh`/`scp`
подключаются без проблем** — KEX-инцидент был специфичен для старого хоста.
`plink`/`pscp` тоже работают и остаются рабочим вариантом для неинтерактивных
батч-команд с пиненным host key.

## Вход по ключу (работает сразу, пароль не нужен)

Публичные ключи уже добавлены в `~/.ssh/authorized_keys` на новом сервере:
- `~/.ssh/tbank_proxy_deploy.pub` (ed25519)
- `~/.ssh/tbank_proxy_deploy_rsa.pub` (RSA, PEM)

```bash
ssh -i ~/.ssh/tbank_proxy_deploy root@72.56.8.174 "uname -a"
scp -i ~/.ssh/tbank_proxy_deploy local_file root@72.56.8.174:/opt/tbank_proxy/
```

## Рабочая команда через plink (неинтерактивно, с закреплённым host key)

Пароль берётся из `.env` (`SSH_PASSWORD`). Пример выполнения команды на сервере:

```powershell
$hk = "SHA256:KqYZBEnG/cqrBqZSWmJzlS2yo2I4K6+uzCtcX6dKQhQ"
& "C:\Program Files\PuTTY\plink.exe" -ssh -batch -hostkey $hk root@72.56.8.174 -pw <ПАРОЛЬ_ИЗ_.env> "uname -a"
```

Вызывать `plink.exe` напрямую через `&` (call operator в PowerShell), НЕ через
`cmd /c '...'` — пароль может содержать символы (`^`, `!`), которые cmd.exe
интерпретирует как управляющие, и тихо портит строку до отправки на сервер.
`-batch` запрещает интерактивные prompt-ы (не зависнет), `-hostkey` пинит ключ
сервера (защита от MITM). Копирование файлов — `pscp` с теми же `-hostkey -pw`.

## Рекомендация по безопасности

Когда вход по ключу настроен и проверен — отключить парольный вход на сервере:
`PasswordAuthentication no` в `/etc/ssh/sshd_config` + `systemctl restart ssh`.
И сменить текущий root-пароль (он лежал в открытом виде).
