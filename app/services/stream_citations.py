"""Remove provider-only inline markers while keeping real citation annotations."""

import re

_OPEN = re.compile("[\ue200【]")
_BRACKET_HEADER = re.compile(r"【[0-9]{1,9}[:：][0-9]{1,9}†")
_BRACKET_PREFIX = re.compile(r"【(?:[0-9]{1,9}(?:[:：][0-9]{0,9})?)?")
_PRIVATE_HEADERS = ("\ue200filecite\ue202", "\ue200cite\ue202")


class StreamCitations:
    def __init__(self) -> None:
        self._pending = ""
        self._closing = ""
        self._offset = 0
        self._removed: list[tuple[int, int]] = []

    def _remove(self, length: int) -> None:
        if self._removed and self._removed[-1][1] == self._offset:
            self._removed[-1] = (self._removed[-1][0], self._offset + length)
        else:
            self._removed.append((self._offset, self._offset + length))
        self._offset += length

    def visible_offset(self, index: int) -> int:
        return index - sum(max(0, min(index, end) - start) for start, end in self._removed)

    def feed(self, delta: str, *, final: bool = False) -> str:
        text = self._pending + delta
        self._pending = ""
        visible = []
        while text:
            if self._closing:
                end = text.find(self._closing)
                length = len(text) if end < 0 else end + 1
                self._remove(length)
                text = text[length:]
                if end >= 0:
                    self._closing = ""
                continue

            marker = _OPEN.search(text)
            length = marker.start() if marker else len(text)
            if length:
                visible.append(text[:length])
                self._offset += length
                text = text[length:]
                continue

            if text.startswith(_PRIVATE_HEADERS):
                self._closing = "\ue201"
            elif _BRACKET_HEADER.match(text):
                self._closing = "】"
            elif any(header.startswith(text) for header in _PRIVATE_HEADERS) or _BRACKET_PREFIX.fullmatch(text):
                if not final:
                    self._pending = text
                elif text.startswith("\ue200"):
                    self._remove(len(text))
                else:
                    visible.append(text)
                    self._offset += len(text)
                break
            else:
                visible.append(text[0])
                self._offset += 1
                text = text[1:]
        return "".join(visible)
