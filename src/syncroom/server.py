from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field

from syncroom.protocol import decode_message, encode_message


@dataclass
class Client:
    client_id: str
    name: str
    room: str
    writer: asyncio.StreamWriter


@dataclass
class RoomState:
    room: str
    clients: dict[str, Client] = field(default_factory=dict)
    password: str = ""
    media_url: str = ""
    position_ms: int = 0
    playing: bool = False
    seek_token: int = 0
    event_id: int = 0
    last_action: str = ""
    updated_at: float = field(default_factory=time.time)
    updated_by: str = ""
    updated_by_name: str = ""

    def snapshot(self) -> dict[str, object]:
        position_ms = self.position_ms
        if self.playing:
            elapsed = int((time.time() - self.updated_at) * 1000)
            position_ms += max(0, elapsed)
        return {
            "type": "room_state",
            "room": self.room,
            "media_url": self.media_url,
            "position_ms": position_ms,
            "playing": self.playing,
            "seek_token": self.seek_token,
            "event_id": self.event_id,
            "last_action": self.last_action,
            "updated_by": self.updated_by,
            "updated_by_name": self.updated_by_name,
            "members": [{"id": c.client_id, "name": c.name} for c in self.clients.values()],
        }


class SyncRoomServer:
    def __init__(self) -> None:
        self.rooms: dict[str, RoomState] = {}
        self._broadcaster_task: asyncio.Task[None] | None = None

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client: Client | None = None
        peer = writer.get_extra_info("peername")
        try:
            join_line = await reader.readline()
            if not join_line:
                return
            join = decode_message(join_line)
            if join.get("type") != "join":
                writer.write(encode_message({"type": "error", "message": "first message must be join"}))
                await writer.drain()
                return

            room_name = str(join.get("room") or "main").strip() or "main"
            client_name = str(join.get("name") or "guest").strip() or "guest"
            room = self.rooms.setdefault(room_name, RoomState(room=room_name))
            supplied_password = str(join.get("password") or "")
            if room.password:
                if supplied_password != room.password:
                    writer.write(
                        encode_message(
                            {"type": "error", "message": "Wrong room password."}
                        )
                    )
                    await writer.drain()
                    return
            elif supplied_password and not room.clients:
                room.password = supplied_password
            client = Client(str(uuid.uuid4()), client_name, room_name, writer)
            room.clients[client.client_id] = client

            writer.write(
                encode_message(
                    {
                        "type": "welcome",
                        "client_id": client.client_id,
                        "room": room_name,
                    }
                )
            )
            writer.write(encode_message(room.snapshot()))
            await writer.drain()
            await self.broadcast_room(room_name)

            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                payload = decode_message(line)
                await self.handle_message(client, payload)
        except (ConnectionError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        finally:
            if client is not None:
                await self.disconnect_client(client)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            print(f"Disconnected {peer}")

    async def handle_message(self, client: Client, payload: dict[str, object]) -> None:
        room = self.rooms.get(client.room)
        if room is None:
            return

        msg_type = payload.get("type")
        if msg_type == "state":
            media_url = str(payload.get("media_url") or room.media_url)
            position_ms = int(payload.get("position_ms") or 0)
            playing = bool(payload.get("playing"))
            force_seek = bool(payload.get("force_seek"))
            reason = str(payload.get("reason") or "").strip().lower()

            room.media_url = media_url
            room.position_ms = max(0, position_ms)
            room.playing = playing
            if force_seek:
                room.seek_token += 1
            room.event_id += 1
            room.last_action = reason
            room.updated_at = time.time()
            room.updated_by = client.client_id
            room.updated_by_name = client.name
            await self.broadcast_room(room.room)
        elif msg_type == "ping":
            client.writer.write(encode_message({"type": "pong"}))
            await client.writer.drain()

    async def broadcast_room(self, room_name: str) -> None:
        room = self.rooms.get(room_name)
        if room is None:
            return
        message = encode_message(room.snapshot())
        stale_clients: list[str] = []
        for client_id, client in room.clients.items():
            try:
                client.writer.write(message)
                await client.writer.drain()
            except (ConnectionError, BrokenPipeError):
                stale_clients.append(client_id)

        for client_id in stale_clients:
            room.clients.pop(client_id, None)

        if not room.clients:
            self.rooms.pop(room_name, None)

    async def disconnect_client(self, client: Client) -> None:
        room = self.rooms.get(client.room)
        if room is None:
            return
        room.clients.pop(client.client_id, None)
        if room.clients:
            await self.broadcast_room(room.room)
        else:
            self.rooms.pop(room.room, None)

    async def periodic_broadcasts(self, interval: float = 0.75) -> None:
        while True:
            await asyncio.sleep(interval)
            active_rooms = [
                room_name
                for room_name, room in self.rooms.items()
                if room.playing and room.clients and room.media_url
            ]
            for room_name in active_rooms:
                await self.broadcast_room(room_name)


async def run_server(host: str, port: int) -> None:
    app = SyncRoomServer()
    server = await asyncio.start_server(app.handle_client, host=host, port=port)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"SyncRoom server listening on {sockets}")
    broadcaster_task = asyncio.create_task(app.periodic_broadcasts())
    async with server:
        try:
            await server.serve_forever()
        finally:
            broadcaster_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await broadcaster_task


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SyncRoom room server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=24873)
    args = parser.parse_args()
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        print("SyncRoom server stopped.")


if __name__ == "__main__":
    main()
