from django import template


register = template.Library()


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return None
    return mapping.get(key)


@register.filter
def initials(value):
    """Возвращает 1-2 заглавные инициалы для имени.

    Примеры:
        "Иванов Иван Иванович" -> "ИИ"
        "Петров П." -> "ПП"
        "alex" -> "A"
        "" -> "?"
    """
    if value is None:
        return '?'
    text = str(value).strip()
    if not text:
        return '?'
    parts = [part for part in text.replace('.', ' ').split() if part]
    if not parts:
        return text[:1].upper()
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[1][:1]).upper()
