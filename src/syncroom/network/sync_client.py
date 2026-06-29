from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from syncroom.protocol import decode_message, encode_message
from syncroom.utils.logging import append_runtime_log


class SyncClient(QObject):
    room_state = Signal(dict)
    info = Signal(str)
    error = Signal(str)
    connected = Signal()
    disconnected = Signal()
    pong = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.socket = QTcpSocket(self)
        self.socket.readyRead.connect(self._on_ready_read)
        self.socket.connected.connect(self._on_socket_connected)
        self.socket.disconnected.connect(self.disconnected)
        self.socket.errorOccurred.connect(self._on_error)
        self._buffer = bytearray()
        self._pending_join_payload: dict | None = None
        self.client_id = ""
        self.room = ""
        self.name = ""

    def connect_to_server(
        self,
        host: str,
        port: int,
        room: str,
        name: str,
        password: str = "",
    ) -> None:
        self.room = room
        self.name = name
        self.client_id = ""
        self._buffer.clear()
        self._pending_join_payload = {
            "type": "join",
            "room": room,
            "name": name,
            "password": password,
        }
        append_runtime_log(
            f"Connecting to server host={host} port={port} room={room} name={name} password={'yes' if password else 'no'}"
        )
        if self.socket.state() != QAbstractSocket.UnconnectedState:
            self.socket.abort()
        self.socket.connectToHost(host, port)

    def disconnect_from_server(self) -> None:
        self._pending_join_payload = None
        self.client_id = ""
        if self.socket.state() != QAbstractSocket.UnconnectedState:
            self.socket.disconnectFromHost()

    def send_state(
        self,
        media_url: str,
        position_ms: int,
        playing: bool,
        force_seek: bool = False,
        reason: str = "",
    ) -> None:
        if self.socket.state() != QAbstractSocket.ConnectedState:
            return
        self.send(
            {
                "type": "state",
                "media_url": media_url,
                "position_ms": position_ms,
                "playing": playing,
                "force_seek": force_seek,
                "reason": reason,
            }
        )

    def send(self, payload: dict) -> None:
        if self.socket.state() != QAbstractSocket.ConnectedState:
            return
        self.socket.write(encode_message(payload))

    def _on_socket_connected(self) -> None:
        if self._pending_join_payload is not None:
            self.send(self._pending_join_payload)

    def _on_ready_read(self) -> None:
        self._buffer.extend(self.socket.readAll().data())
        while b"\n" in self._buffer:
            raw_line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            if not raw_line.strip():
                continue
            try:
                payload = decode_message(raw_line)
            except Exception as exc:
                self.error.emit(f"Invalid message from server: {exc}")
                continue
            self._handle_message(payload)

    def _handle_message(self, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type == "welcome":
            self.client_id = str(payload.get("client_id") or "")
            self._pending_join_payload = None
            append_runtime_log(
                f"Connected to room welcome room={payload.get('room')} client_id={self.client_id}"
            )
            self.info.emit(f"Connected to room {payload.get('room')}")
            self.connected.emit()
        elif msg_type == "room_state":
            self.room_state.emit(payload)
        elif msg_type == "pong":
            self.pong.emit()
        elif msg_type == "error":
            message = str(payload.get("message") or "Unknown server error")
            append_runtime_log(f"Server error: {message}")
            self.error.emit(message)
            if not self.client_id and self.is_join_rejection(message):
                self._pending_join_payload = None
                self.socket.abort()

    def _on_error(self, _socket_error: QAbstractSocket.SocketError) -> None:
        self.error.emit(self.socket.errorString())

    @staticmethod
    def is_join_rejection(message: str) -> bool:
        lowered = message.strip().lower()
        return any(fragment in lowered for fragment in ("password", "auth", "join", "first message"))
