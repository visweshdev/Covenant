from dataclasses import dataclass


@dataclass
class Doc:
    text: str
    strict: bool = False


def parse(text: str, strict: bool = False) -> Doc:
    return Doc(text=text.strip(), strict=strict)
