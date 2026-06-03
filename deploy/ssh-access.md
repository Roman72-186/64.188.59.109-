# SSH-доступ к VPS

> Параметры и пароль — в `.env` (он в `.gitignore`). Здесь только как подключаться.

## Сервер
- IP: `64.188.59.109`, порт `22`, пользователь `root`
- ОС: **Ubuntu 22.04.5 LTS**, системный **Python 3.10.12** (подходит под требование 3.10+)
- Host key (ssh-ed25519): `SHA256:hjpEz+t9llOppnKcoZDzjm6hu8H9F/a1c/nLw/A4myk`

## Важно: на этой Windows-машине работает только plink (PuTTY)

Штатный Windows-OpenSSH (`ssh.exe`, `ssh-keyscan`) **не подключается** к этому
серверу — падает на согласовании KEX (`unsupported KEX method
sntrup761x25519-sha512@openssh.com`). PuTTY использует свою криптографию и
подключается нормально. Поэтому для деплоя/команд используем `plink`/`pscp`.

## Рабочая команда (неинтерактивно, с закреплённым host key)

Пароль берётся из `.env` (`SSH_PASSWORD`). Пример выполнения команды на сервере:

```powershell
$hk = "SHA256:hjpEz+t9llOppnKcoZDzjm6hu8H9F/a1c/nLw/A4myk"
cmd /c '"C:\Program Files\PuTTY\plink.exe" -ssh -batch -hostkey ' + $hk +
       ' root@64.188.59.109 -pw <ПАРОЛЬ_ИЗ_.env> "uname -a"'
```

`-batch` запрещает интерактивные prompt-ы (не зависнет), `-hostkey` пинит ключ
сервера (защита от MITM). Копирование файлов — `pscp` с теми же `-hostkey -pw`.

## Вход по ключу

Публичные ключи уже добавлены в `~/.ssh/authorized_keys` на сервере:
- `~/.ssh/tbank_proxy_deploy.pub` (ed25519)
- `~/.ssh/tbank_proxy_deploy_rsa.pub` (RSA, PEM)

- **Из Linux/OpenSSH** — вход по ключу работает сразу: `ssh -i ~/.ssh/tbank_proxy_deploy root@64.188.59.109`.
- **Через plink (Windows)** — plink 0.84 принимает только формат `.ppk`. Чтобы
  ходить по ключу без пароля, открыть приватный ключ в **PuTTYgen (GUI)** →
  *Save private key* → `tbank_proxy_deploy.ppk`, затем `plink -i ...ppk`.
  Headless-конвертация puttygen на этой машине не отрабатывает, поэтому шаг ручной.

## Рекомендация по безопасности

Когда вход по ключу настроен и проверен — отключить парольный вход на сервере:
`PasswordAuthentication no` в `/etc/ssh/sshd_config` + `systemctl restart ssh`.
И сменить текущий root-пароль (он лежал в открытом виде).
