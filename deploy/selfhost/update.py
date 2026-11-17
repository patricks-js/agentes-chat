#!/usr/bin/env python3
"""Release updates for an existing Docker Compose self-host installation."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request


def version(tag):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, match.groups())) if match else None


def latest(repo):
    releases = []
    # Desktop/setup tags share this repository's releases feed.
    for page in range(1, 11):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "agentes-selfhost-updater"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.load(response)
        releases.extend(r["tag_name"] for r in batch if not r["draft"] and not r["prerelease"] and version(r["tag_name"]))
        if len(batch) < 100:
            break
    if not releases:
        raise RuntimeError("No stable server release found")
    return max(releases, key=version)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def compose(*args, **kwargs):
    return run("docker", "compose", *args, **kwargs)


def write_image(path, image):
    text = path.read_text()
    line = f"AGENTES_IMAGE={image}"
    text, count = re.subn(r"^AGENTES_IMAGE=.*$", lambda _: line, text, flags=re.M)
    if not count:
        text = text.rstrip("\n") + "\n" + line + "\n"
    temporary = path.with_name(".env.update-tmp")
    temporary.write_text(text)
    temporary.chmod(0o600)
    temporary.replace(path)


def update(directory, tag, image_repo, check=False):
    os.chdir(directory)
    container = output("docker", "compose", "ps", "-q", "agentes")
    if not container:
        raise RuntimeError("The agentes service must be running")
    current = json.loads(output("docker", "inspect", container))[0]
    if current["State"].get("Health", {}).get("Status") != "healthy":
        raise RuntimeError("Current application is not healthy; refusing automatic update")
    health = json.loads(output("docker", "exec", container, "bun", "-e",
        'fetch("http://127.0.0.1:"+(process.env.PORT||3000)+"/api/health").then(r=>{if(!r.ok)process.exit(1);return r.text()}).then(console.log)'))
    installed = version(health["version"])
    if installed is None:
        raise RuntimeError("Cannot compare installed version")
    if version(tag) <= installed:
        print(f"Already current: {health['version']} (latest {tag})", flush=True)
        return
    image = f"{image_repo}:{tag.removeprefix('v')}"
    print(f"Update available: {health['version']} -> {tag} ({image})", flush=True)
    if check:
        return
    # Download before downtime. Compose/postgres are deliberately held at their installed versions.
    run("docker", "pull", image)
    target = output("docker", "image", "inspect", "--format", "{{.Id}}", image)
    mounts = [m for m in current["Mounts"] if m["Destination"] == "/data/files"]
    if len(mounts) != 1 or mounts[0]["Type"] != "volume":
        raise RuntimeError("Automatic backup requires a named volume mounted at /data/files")
    volume = mounts[0]["Name"]
    backup = directory / "backups" / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + tag)
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(".env", backup / "env")
    (backup / "env").chmod(0o600)
    (backup / "metadata.json").write_text(json.dumps({"previous_image": current["Image"], "target_image": target, "release": tag}, indent=2))
    stopped = False
    changed = False
    try:
        # Set before stop: even an interrupted/failed stop must attempt to restart the old app.
        stopped = True
        compose("stop", "agentes")
        with (backup / "database.dump").open("wb") as dump:
            compose("exec", "-T", "postgres", "pg_dump", "-U", "agentes", "-d", "agentes", "-Fc", stdout=dump)
        run("docker", "run", "--rm", "--user", "root", "--entrypoint", "tar",
            "-v", f"{volume}:/files:ro", "-v", f"{backup}:/backup", current["Image"],
            "-czf", "/backup/files.tar.gz", "-C", "/files", ".")
        changed = True
        write_image(Path(".env"), target)
        compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "300", "agentes")
        print(f"Updated to {tag}; backup: {backup}", flush=True)
    except BaseException:
        if changed:
            print("Update failed; restoring the database, files and previous image", file=sys.stderr, flush=True)
            # If recovery fails, leave the application stopped and keep the dump for manual recovery.
            compose("stop", "agentes")
            compose("exec", "-T", "postgres", "psql", "-U", "agentes", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                "-c", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'agentes' AND pid <> pg_backend_pid()")
            with (backup / "database.dump").open("rb") as dump:
                compose("exec", "-T", "postgres", "pg_restore", "-U", "agentes", "-d", "postgres", "--clean", "--if-exists", "--create", "--exit-on-error", stdin=dump)
            run("docker", "run", "--rm", "--user", "root", "--entrypoint", "sh",
                "-v", f"{volume}:/files", "-v", f"{backup}:/backup:ro", current["Image"], "-ec",
                'find /files -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; tar -xzf /backup/files.tar.gz -C /files')
            shutil.copy2(backup / "env", ".env")
            # Pin the actual old image even if its original tag has moved.
            write_image(Path(".env"), current["Image"])
        if stopped:
            compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "300", "agentes")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--repository", default="agentes-chat/agentes-chat")
    parser.add_argument("--image-repository", default="ghcr.io/agentes-chat/agentes")
    parser.add_argument("--check", action="store_true", help="Report available updates without installing")
    args = parser.parse_args()
    directory = args.directory.resolve()
    os.umask(0o077)
    with (directory / ".update.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another updater is running")
            return
        update(directory, latest(args.repository), args.image_repository, args.check)


if __name__ == "__main__":
    main()
