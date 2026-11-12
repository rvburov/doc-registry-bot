"""Тело события потока службы мессенджера: строки сообщений в любом вложении списков."""

import json

from doc_registry_bot.messenger.client import event_items

ROW = {"ownerId": 5001, "serverMessageId": 7, "text": "проверка"}


def body(data: object) -> str:
    return json.dumps({"type": "INCOMING_MESSAGE", "data": data}, ensure_ascii=False)


def test_rows_in_a_list_of_lists_are_read() -> None:
    # Так присылает настоящая служба: "data": [[{...}]].
    assert event_items(body([[ROW]])) == ("INCOMING_MESSAGE", [ROW])


def test_rows_in_a_plain_list_or_alone_are_read() -> None:
    assert event_items(body([ROW])) == ("INCOMING_MESSAGE", [ROW])
    assert event_items(body(ROW)) == ("INCOMING_MESSAGE", [ROW])


def test_broken_body_gives_nothing() -> None:
    assert event_items("не json") == ("", [])
