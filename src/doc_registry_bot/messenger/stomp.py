"""Кадры STOMP: те же, что посылал SmartBase (CONNECT с heart-beat 0,0, SUBSCRIBE с ack auto)."""

from dataclasses import dataclass

NUL = "\x00"


def frame(command: str, *headers: tuple[str, str], body: str = "") -> str:
    lines = [command, *(f"{name}:{value}" for name, value in headers)]
    return "\n".join(lines) + "\n\n" + body + NUL


def greeting(host: str, topics: list[str]) -> list[str]:
    frames = [
        frame("CONNECT", ("accept-version", "1.2,1.1,1.0"), ("host", host), ("heart-beat", "0,0"))
    ]
    frames += [
        frame("SUBSCRIBE", ("id", f"sub-{number}"), ("destination", topic), ("ack", "auto"))
        for number, topic in enumerate(topics)
    ]
    return frames


@dataclass(frozen=True)
class Frame:
    command: str
    headers: dict[str, str]
    body: str


def parse(text: str) -> list[Frame]:
    """Кадры из пришедшего текста; в одном сообщении WebSocket их может быть несколько."""
    frames: list[Frame] = []
    for raw in text.replace("\r\n", "\n").split(NUL):
        chunk = raw.lstrip("\n")
        if not chunk:
            continue
        split = chunk.find("\n\n")
        head = chunk if split < 0 else chunk[:split]
        body = "" if split < 0 else chunk[split + 2 :]
        lines = head.split("\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            colon = line.find(":")
            if colon <= 0:
                continue
            # Повтор заголовка: по правилам STOMP действует первый.
            headers.setdefault(line[:colon].lower(), line[colon + 1 :])
        frames.append(Frame(lines[0].strip(), headers, body))
    return frames
