from __future__ import annotations

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
    CONTROL_METHODS = {"setMyCommands"}

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
        del method, read_timeout, write_timeout, connect_timeout, pool_timeout
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
            result = self._sent_message(api_method, parameters)
        elif api_method in self.CONTROL_METHODS:
            result = True
        else:
            raise AssertionError(f"Unexpected Bot API method: {api_method}")

        return 200, json.dumps({"ok": True, "result": result}).encode()

    def _sent_message(self, method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self._next_message_id += 1
        result: dict[str, Any] = {
            "message_id": self._next_message_id,
            "date": 1_754_000_000,
            "chat": {"id": parameters["chat_id"], "type": "private"},
            "from": self._bot_user,
        }
        caption = parameters.get("caption")
        if caption is not None:
            result["caption"] = caption

        if method == "sendMessage":
            result["text"] = parameters["text"]
        elif method == "sendPhoto":
            result["photo"] = [
                {
                    **self._file(parameters.get("photo", "photo")),
                    "width": 640,
                    "height": 360,
                }
            ]
        elif method == "sendSticker":
            result["sticker"] = {
                **self._file(parameters.get("sticker", "sticker")),
                "type": "regular",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
            }
        elif method == "sendVoice":
            result["voice"] = {
                **self._file(parameters.get("voice", "voice")),
                "duration": 1,
            }
        elif method == "sendDocument":
            result["document"] = self._file(parameters.get("document", "document"))
        elif method == "sendVideo":
            result["video"] = {
                **self._file(parameters.get("video", "video")),
                "duration": 1,
                "width": 640,
                "height": 360,
            }
        elif method == "sendVideoNote":
            result["video_note"] = {
                **self._file(parameters.get("video_note", "video_note")),
                "duration": 1,
                "length": 360,
            }
        return result

    @staticmethod
    def _file(value: Any) -> dict[str, Any]:
        file_id = value if isinstance(value, str) else "uploaded-test-file"
        return {"file_id": file_id, "file_unique_id": f"unique-{file_id}"}

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
        username: str,
        *,
        timestamp: int = 1_754_000_000,
    ) -> None:
        self.application = application
        self.user_id = user_id
        self.username = username
        self._next_update_id = user_id * 1_000
        self._timestamp = timestamp
        self._last_update: Update | None = None

    async def send(self, text: str) -> None:
        await self.send_message(text=text)

    async def send_message(
        self,
        *,
        text: str | None = None,
        photo: str | None = None,
        sticker: str | None = None,
        voice: str | None = None,
        document: str | None = None,
        video: str | None = None,
        video_note: str | None = None,
        caption: str | None = None,
    ) -> None:
        supplied = [
            value is not None
            for value in (text, photo, sticker, voice, document, video, video_note)
        ]
        if sum(supplied) != 1:
            raise ValueError("Supply exactly one supported content value")

        self._next_update_id += 1
        self._timestamp += 1
        message: dict[str, Any] = {
            "message_id": self._next_update_id,
            "date": self._timestamp,
            "chat": {
                "id": self.user_id,
                "type": "private",
                "first_name": "Test",
                "username": self.username,
            },
            "from": {
                "id": self.user_id,
                "is_bot": False,
                "first_name": "Test",
                "username": self.username,
            },
        }
        if text is not None:
            message["text"] = text
            if text.startswith("/"):
                command = text.split(maxsplit=1)[0]
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        elif photo is not None:
            message["photo"] = [
                {
                    "file_id": photo,
                    "file_unique_id": f"unique-{photo}",
                    "width": 640,
                    "height": 360,
                }
            ]
        elif sticker is not None:
            message["sticker"] = {
                "file_id": sticker,
                "file_unique_id": f"unique-{sticker}",
                "type": "regular",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
            }
        elif voice is not None:
            message["voice"] = {
                "file_id": voice,
                "file_unique_id": f"unique-{voice}",
                "duration": 1,
            }
        elif document is not None:
            message["document"] = {
                "file_id": document,
                "file_unique_id": f"unique-{document}",
            }
        elif video is not None:
            message["video"] = {
                "file_id": video,
                "file_unique_id": f"unique-{video}",
                "duration": 1,
                "width": 640,
                "height": 360,
            }
        elif video_note is not None:
            message["video_note"] = {
                "file_id": video_note,
                "file_unique_id": f"unique-{video_note}",
                "duration": 1,
                "length": 360,
            }
        if caption is not None:
            message["caption"] = caption

        update = Update.de_json(
            {"update_id": self._next_update_id, "message": message},
            self.application.bot,
        )
        self._last_update = update
        await self.application.process_update(update)

    async def replay_last_update(self) -> None:
        if self._last_update is None:
            raise RuntimeError("No Telegram update is available to replay")
        await self.application.process_update(self._last_update)
