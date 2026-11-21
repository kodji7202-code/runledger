"""Terminal display-width measurement and word wrapping."""
import re
import unicodedata


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
               for char in text if unicodedata.category(char) not in ("Mn", "Me"))


def wrap_text(text: str, width: int = 80, initial_indent: str = "",
              subsequent_indent: str = "") -> str:
    if display_width(initial_indent + text) <= width:
        return initial_indent + text
    lines = []
    line, columns = initial_indent, display_width(initial_indent)
    line_has_word = bool(initial_indent.strip())
    continuation_width = display_width(subsequent_indent)
    text = text.expandtabs().translate(str.maketrans("\t\n\v\f\r", "     "))
    end = 0
    for match in re.finditer(r"[^ ]+", text):
        gap, word = text[end:match.start()], match.group()
        end = match.end()
        word_width = display_width(word)
        if line_has_word and columns + len(gap) + word_width > width:
            lines.append(line)
            line, columns = subsequent_indent + word, continuation_width + word_width
        else:
            line += gap + word
            columns += len(gap) + word_width
        line_has_word = True
    return "\n".join([*lines, line])
