"""The store's policies, split into sections, with a simple keyword search.

This is the retrieval the support agent cites. It ranks the policy sections by the query words they
contain (with light stemming), which is enough for one store's few pages; a vector index over many
merchants' documents can replace ``search`` without changing the agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

_STOP = frozenset("a an and are be can do does for how i if in is it my of on or the to what when where "
                  "will with you your me this that".split())


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


@dataclass(frozen=True)
class Section:
    title: str
    text: str
    source: str


class Policies:
    def __init__(self, markdown: str, source: str):
        self.sections: list[Section] = []
        for block in re.split(r"^## ", markdown, flags=re.M)[1:]:
            title, _, body = block.partition("\n")
            self.sections.append(Section(title.strip(), " ".join(body.split()), f"{source}#{title.strip().lower()}"))

    @classmethod
    def northwind(cls) -> Policies:
        text = resources.files("shipjack").joinpath("data/northwind_policies.md").read_text()
        return cls(text, "northwind-policies")

    async def search(self, query: str, k: int = 2) -> list[dict]:
        want = _words(query)
        scored = sorted(((len(want & _words(f"{s.title} {s.title} {s.text}")), i, s)
                         for i, s in enumerate(self.sections)), key=lambda t: (-t[0], t[1]))
        return [{"title": s.title, "text": s.text, "source": s.source} for score, _, s in scored[:k] if score > 0]
