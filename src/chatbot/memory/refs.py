"""Typed record references: `msg:12`, `rec:3`, `psg:40`, `note:7`, `ckpt:2`."""

import re
from dataclasses import dataclass
from typing import Literal

RefType = Literal["msg", "rec", "psg", "note", "ckpt"]
_REF = re.compile(r"^(msg|rec|psg|note|ckpt):([1-9]\d{0,17})$")
EVIDENCE: frozenset[str] = frozenset({"msg", "psg"})


@dataclass(frozen=True)
class Ref:
    type: RefType
    id: int

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"


def parse(ref: str) -> Ref | None:
    """The parsed ref, or None if malformed (callers report malformed like unknown)."""
    m = _REF.match(ref.strip())
    return Ref(m.group(1), int(m.group(2))) if m else None  # type: ignore[arg-type]


def msg(id_: int) -> str:
    return f"msg:{id_}"


def rec(id_: int) -> str:
    return f"rec:{id_}"


def note(id_: int) -> str:
    return f"note:{id_}"
