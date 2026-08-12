import json
from typing import Any

from telegram import Update
from telegram.request import BaseRequest, RequestData


class FakeTelegramRequest(BaseRequest):
    """A small in-memory Bot API transport suitable for PTB integration tests."""

    SEND_METHODS = {
        "sendMessage",
        "sendPhoto",
        "sendSticker",
        "sendVoice",
        "sendDocument",
        "sendVideo",
        "sendVideoNote",
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self._next_message_id = 100
        self._bot_user = {
            "id": 999_001,
            "is_bot": True,
            "first_name": "Quest test bot",
            "username": "quest_test_bot",
        }

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = BaseRequest.DEFAULT_NONE,
        write_timeout: Any = BaseRequest.DEFAULT_NONE,
        connect_timeout: Any = BaseRequest.DEFAULT_NONE,
        pool_timeout: Any = BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        api_method = url.rsplit("/", maxsplit=1)[-1]
        parameters = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((api_method, parameters))

        queued_failures = self.failures.get(api_method)
        if queued_failures:
            status_code, response = queued_failures.pop(0)
            return status_code, json.dumps(response).encode()

        if api_method == "getMe":
            result: Any = self._bot_user
        elif api_method in self.SEND_METHODS:
            result = self._sent_message(parameters)
        elif api_method == "setMyCommands":
            result = True
        else:
            raise AssertionError(f"Unexpected Bot API method: {api_method}")

        return 200, json.dumps({"ok": True, "result": result}).encode()

    def _sent_message(self, parameters: dict[str, Any]) -> dict[str, Any]:
        self._next_message_id += 1
        return {
            "message_id": self._next_message_id,
            "date": 1_754_000_000,
            "chat": {"id": parameters["chat_id"], "type": "private"},
            "from": self._bot_user,
        }

    def calls_to(self, user_id: int) -> list[tuple[str, dict[str, Any]]]:
        return [
            (method, parameters)
            for method, parameters in self.calls
            if method in self.SEND_METHODS and parameters.get("chat_id") == user_id
        ]

    def messages_to(self, user_id: int) -> list[str]:
        return [
            str(parameters["text"])
            for method, parameters in self.calls_to(user_id)
            if method == "sendMessage"
        ]

    def clear(self) -> None:
        self.calls.clear()

    def fail_next(
        self,
        api_method: str,
        *,
        error_code: int = 500,
        description: str = "Synthetic Telegram failure",
        retry_after: int | None = None,
    ) -> None:
        response: dict[str, Any] = {
            "ok": False,
            "error_code": error_code,
            "description": description,
        }
        if retry_after is not None:
            response["parameters"] = {"retry_after": retry_after}
        self.failures.setdefault(api_method, []).append((error_code, response))


class TelegramUser:
    def __init__(
        self,
        application: Any,
        user_id: int,
        username: str | None,
        *,
        timestamp: int = 1_754_000_000,
    ) -> None:
        self.application = application
        self.user_id = user_id
        self.username = username
        self._next_update_id = user_id * 1_000
        self._timestamp = timestamp
        self._last_update: Update | None = None
        self._last_message: dict[str, Any] | None = None

    async def send(self, text: str) -> None:
        await self.send_message(text=text)

    def _new_message(self) -> dict[str, Any]:
        self._next_update_id += 1
        self._timestamp += 1
        chat: dict[str, Any] = {
            "id": self.user_id,
            "type": "private",
            "first_name": "Test",
        }
        sender: dict[str, Any] = {
            "id": self.user_id,
            "is_bot": False,
            "first_name": "Test",
        }
        if self.username is not None:
            chat["username"] = self.username
            sender["username"] = self.username
        return {
            "message_id": self._next_update_id,
            "date": self._timestamp,
            "chat": chat,
            "from": sender,
        }

    async def _process_message(self, message: dict[str, Any]) -> None:
        update = Update.de_json(
            {"update_id": self._next_update_id, "message": message},
            self.application.bot,
        )
        self._last_update = update
        self._last_message = message
        await self.application.process_update(update)

    async def send_message(
        self,
        *,
        text: str | None = None,
        document: str | None = None,
        caption: str | None = None,
    ) -> None:
        if (text is None) == (document is None):
            raise ValueError("Supply exactly one supported content value")

        message = self._new_message()
        if text is not None:
            message["text"] = text
            if text.startswith("/"):
                command = text.split(maxsplit=1)[0]
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        elif document is not None:
            message["document"] = {
                "file_id": document,
                "file_unique_id": f"unique-{document}",
            }
        if caption is not None:
            message["caption"] = caption

        await self._process_message(message)

    async def share_user(
        self,
        *,
        request_id: int,
        user_id: int,
        first_name: str,
        last_name: str | None = None,
        username: str | None = None,
    ) -> None:
        shared_user: dict[str, Any] = {
            "user_id": user_id,
            "first_name": first_name,
        }
        if last_name is not None:
            shared_user["last_name"] = last_name
        if username is not None:
            shared_user["username"] = username
        message = self._new_message()
        message["users_shared"] = {
            "request_id": request_id,
            "users": [shared_user],
        }
        await self._process_message(message)

    async def edit_last_message(self, text: str) -> None:
        if self._last_message is None or "text" not in self._last_message:
            raise RuntimeError("No text message is available to edit")
        self._next_update_id += 1
        self._timestamp += 1
        message: dict[str, Any] = {
            **self._last_message,
            "text": text,
            "edit_date": self._timestamp,
        }
        message.pop("entities", None)
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        update = Update.de_json(
            {"update_id": self._next_update_id, "edited_message": message},
            self.application.bot,
        )
        self._last_update = update
        self._last_message = message
        await self.application.process_update(update)

    async def replay_last_update(self) -> None:
        if self._last_update is None:
            raise RuntimeError("No Telegram update is available to replay")
        await self.application.process_update(self._last_update)
