import json
import logging
from uuid import uuid4

from PySide6.QtCore import QEventLoop, QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

class WatchdogIpcServer(QObject):
    SERVER_NAME = "botstudio-watchdog-v1"
    MAX_MESSAGE_BYTES = 64 * 1024

    def __init__(self, request_handler, parent=None, server_name=None, logger=None):
        super().__init__(parent)
        self.request_handler = request_handler
        self.server_name = server_name or self.SERVER_NAME
        self.logger = logger
        self.server = QLocalServer(self)
        self.server.setSocketOptions(
            QLocalServer.SocketOption.UserAccessOption
        )
        self.server.setMaxPendingConnections(5)
        self.server.newConnection.connect(self._accept_connections)
        self._buffers = {}

    def start(self):
        if self.server.isListening():
            return
        if not self.server.listen(self.server_name):
            raise OSError(self.server.errorString())

    def stop(self):
        self.server.close()
        for socket in list(self._buffers):
            socket.abort()
            socket.deleteLater()
        self._buffers.clear()

    def _accept_connections(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(
                lambda current_socket=socket: self._read_request(current_socket)
            )
            socket.disconnected.connect(
                lambda current_socket=socket: self._forget_socket(current_socket)
            )
            if socket.bytesAvailable():
                self._read_request(socket)

    def _read_request(self, socket):
        buffer = self._buffers.get(socket)
        if buffer is None:
            return

        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > self.MAX_MESSAGE_BYTES:
            self._respond(socket, None, error="Request is too large")
            return

        newline = buffer.find(b"\n")
        if newline < 0:
            return
        if newline != len(buffer) - 1:
            self._respond(socket, None, error="Only one request is allowed")
            return

        request_id = None
        try:
            request = json.loads(buffer[:newline].decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            request_id = request.get("id")
            if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                raise ValueError("Request id must be a non-empty string of at most 128 characters")
            if not {"id", "command"}.issubset(request) or set(request) - {
                "id",
                "command",
                "arguments",
            }:
                raise ValueError("Request contains invalid fields")
            if not isinstance(request["command"], str):
                raise ValueError("Command must be a string")
            arguments = request.get("arguments")
            if arguments is not None and not isinstance(arguments, dict):
                raise ValueError("Command arguments must be an object")
            if arguments is None:
                result = self.request_handler(request["command"])
            else:
                result = self.request_handler(
                    request["command"],
                    arguments,
                )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as ex:
            self._respond(socket, request_id, error=str(ex))
            return
        except Exception as ex:
            self._respond(socket, request_id, error="Control request failed")
            self._log_error("Watchdog IPC Request Failed", ex)
            return

        self._respond(socket, request_id, result=result)

    def _respond(self, socket, request_id, result=None, error=None):
        response = {
            "id": request_id,
            "ok": error is None,
        }
        if error is None:
            response["result"] = result
        else:
            response["error"] = error
        try:
            payload = json.dumps(
                response,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as ex:
            self._log_error("Watchdog IPC Response Serialization Failed", ex)
            payload = (
                json.dumps(
                    {
                        "id": request_id,
                        "ok": False,
                        "error": "IPC response could not be serialized",
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        if len(payload) > self.MAX_MESSAGE_BYTES:
            payload = (
                json.dumps(
                    {
                        "id": request_id,
                        "ok": False,
                        "error": "IPC response is too large",
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        if socket.write(payload) < 0:
            self._log_error(
                "Watchdog IPC Response Write Failed",
                socket.errorString(),
            )
            socket.abort()
            return
        socket.disconnectFromServer()

    def _log_error(self, message, error):
        if self.logger is not None:
            self.logger.error(message, error=str(error))
            return
        logging.getLogger("botstudio.watchdog_ipc").error(
            "%s: %s",
            message,
            error,
        )

    def _forget_socket(self, socket):
        self._buffers.pop(socket, None)
        socket.deleteLater()


class WatchdogIpcClient:
    MAX_MESSAGE_BYTES = WatchdogIpcServer.MAX_MESSAGE_BYTES
    MAX_TIMEOUT_MS = 10_000

    def __init__(self, server_name=None):
        self.server_name = server_name or WatchdogIpcServer.SERVER_NAME

    def request(
        self,
        command,
        timeout_ms=3_000,
        request_id=None,
        arguments=None,
    ):
        if not isinstance(command, str) or not command:
            raise ValueError("Command must be a non-empty string")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= self.MAX_TIMEOUT_MS:
            raise ValueError(
                f"Timeout must be between 1 and {self.MAX_TIMEOUT_MS} milliseconds"
            )

        request_id = request_id or uuid4().hex
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("Request id must be a non-empty string of at most 128 characters")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("Command arguments must be an object")
        request = {"id": request_id, "command": command}
        if arguments is not None:
            request["arguments"] = arguments
        payload = json.dumps(
            request,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(payload) > self.MAX_MESSAGE_BYTES:
            raise ValueError("Request is too large")

        socket = QLocalSocket()
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        response = bytearray()
        result = {}

        def fail(error):
            result["error"] = error
            loop.quit()

        def send():
            if socket.write(payload) != len(payload):
                fail(OSError(socket.errorString()))

        def read_response():
            response.extend(bytes(socket.readAll()))
            if len(response) > self.MAX_MESSAGE_BYTES:
                fail(ValueError("IPC response is too large"))
                return
            if b"\n" not in response:
                return
            line, remainder = response.split(b"\n", 1)
            if remainder:
                fail(ValueError("IPC response contains trailing data"))
                return
            try:
                decoded = json.loads(line.decode("utf-8"))
                if not isinstance(decoded, dict) or decoded.get("id") != request_id:
                    raise ValueError("IPC response does not match the request")
                if decoded.get("ok") is not True:
                    raise RuntimeError(decoded.get("error", "IPC request failed"))
                result["value"] = decoded.get("result")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeError) as ex:
                fail(ex)
                return
            loop.quit()

        def handle_socket_error(_socket_error):
            if "value" not in result and "error" not in result:
                fail(ConnectionError(socket.errorString()))

        timeout.timeout.connect(
            lambda: fail(TimeoutError("Timed out waiting for IPC response"))
        )
        socket.connected.connect(send)
        socket.readyRead.connect(read_response)
        socket.errorOccurred.connect(handle_socket_error)
        try:
            socket.connectToServer(self.server_name)
            timeout.start(timeout_ms)
            loop.exec()
            if "error" in result:
                raise result["error"]
            return result["value"]
        finally:
            timeout.stop()
            socket.abort()
