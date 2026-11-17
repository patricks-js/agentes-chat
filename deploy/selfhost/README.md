# Automatic updates for Docker Compose

`update.py` checks stable server releases (`vX.Y.Z`, excluding desktop/setup and prereleases),
pulls the versioned application image before stopping the service, backs up Postgres and the
files volume, starts the new image and waits for Docker's health check. A failed start restores
the database, files and actual previous image. Database recovery must succeed before restarting.

Requires Linux, Python 3, Docker Compose v2+, a running healthy `agentes` service, the `postgres`
service with database/user `agentes`, `.env`, and a named volume at `/data/files`. Run as the
installation's owner with Docker access. Backups contain secrets; keep them private.

```sh
python3 deploy/selfhost/update.py --directory /home/patrick/docker/agentes --check
python3 deploy/selfhost/update.py --directory /home/patrick/docker/agentes
```

The default source is `agentes-chat/agentes-chat` and `ghcr.io/agentes-chat/agentes`.
For a fork that publishes versioned images, set `--repository owner/repo` and
`--image-repository ghcr.io/owner/image`. Updating from official images replaces local code
changes. No update is installed when the running version equals or exceeds the latest release.

The updater preserves Compose and the Postgres image: changes to infrastructure, Postgres major
upgrades and new release requirements need manual review. Additional workers or other database
clients must be stopped separately; this tool targets the single app Compose installation.
Backups are retained under the installation's `backups/` directory without automatic deletion.
If restore fails, the app stays stopped; inspect the updater log and recover using the saved dump,
files archive and `metadata.json`. Run only one deployment mechanism at a time.

For unattended installation, put the script at a fixed path, then create a systemd user oneshot
service with `ExecStart=/usr/bin/python3 /absolute/path/update.py --directory /absolute/install/path`
and a timer with `OnCalendar=*-*-* 04:00:00 America/Recife`, `Persistent=true`, and
`RandomizedDelaySec=15m`. Enable user lingering so it runs without an active login.
Use `journalctl --user -u agentes-update.service` to inspect updates and failures.
