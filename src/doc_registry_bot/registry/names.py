"""ФИО регистрирующего."""


def is_full_name(fio: str | None) -> bool:
    """ФИО написано полностью: ровно три слова, в каждом не меньше двух букв.

    Инициалы («Иванов И.И.») не проходят намеренно: журнал читают люди, которые регистратора лично
    не знают, а по инициалам однофамильцы неразличимы. Дефис внутри слова разрешен.
    """
    parts = (fio or "").split()
    if len(parts) != 3:
        return False
    for part in parts:
        if len(part) < 2 or part[0] == "-" or part[-1] == "-":
            return False
        if not all(symbol.isalpha() or symbol == "-" for symbol in part):
            return False
    return True
