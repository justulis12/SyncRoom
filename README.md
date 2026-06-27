# SyncRoom

Simple synced watch rooms with a small server and local `mpv` playback.

## Server on Raspberry Pi

Clone the repo:

```bash
git clone https://github.com/justulis12/SyncRoom.git
cd SyncRoom
```

Start the server:

```bash
docker compose up -d --build
```

Default port is `24873`.

If you want a different port, edit `docker-compose.yml` and change the port there too.

Port forward that TCP port on your router to the Pi.

## Linux client

Install what you need:

```bash
sudo pacman -S python python-pyside6 mpv
```

Install SyncRoom:

```bash
./scripts/install-linux.sh
```

Run it:

```bash
syncroom
```

## Windows client

Use the installer from GitHub Actions / Releases.

On first launch, SyncRoom installs `mpv` automatically.

## In the app

1. Enter host, port, room, and name.
2. Join the room.
3. Paste a direct video URL.
4. Press play.

## Notes

- The video link needs to be a direct streamable file URL.
- Audio selection is local per user.
- The server only syncs state. Playback happens on each client.
