# domain/context/engine.py
from __future__ import annotations
import asyncio
from domain.context.providers import ContextProvider
from domain.memory.short.default_short_term_memory import ShortTermMemory


class ContextEngine:

    def __init__(
        self,
        providers: list[ContextProvider],
        memory:    ShortTermMemory,
        *, summarize=None, notify=None,
    ) -> None:
        self._providers = providers
        self._memory    = memory
        self.summarize, self.notify = summarize, notify
        self._prepare_lock = asyncio.Lock()
        self.last_sections = []
        self.last_text = ""
        self.last_state = {}

    async def prepare(self, state: dict) -> None:
        async with self._prepare_lock:
            for provider in self._providers:
                if provider.enabled and getattr(provider, '_field', None) == 'tool_respond':
                    await provider._strategy.prepare(self._memory, 'tool_respond', state,
                        summarize=self.summarize, notify=self.notify)

    def build(self, state: dict) -> str:
        self.last_sections = []
        self.last_state = state
        for p in self._providers:
            if not p.enabled:
                continue
            try:
                text = "\n\n".join(piece for piece in p.get(state) if piece.strip())
                if text:
                    self.last_sections.append({"name": p.name, "text": text, "provider": p,
                                               "tool_names": list(getattr(p, 'last_tool_names', []))})
            except Exception as e:
                name = p.name if hasattr(p, "name") else type(p).__name__
                print(f"[ContextEngine] provider '{name}' error: {e}")
        self.last_text = "\n\n".join(section['text'] for section in self.last_sections)
        return self.last_text

    def refresh_tool_sections(self) -> str:
        """Refresh tools on a retry, without consuming other providers again."""
        for section in self.last_sections:
            provider = section['provider']
            if getattr(provider, '_field', None) == 'tool_respond':
                section['text'] = "\n\n".join(provider.get(self.last_state))
        self.last_text = "\n\n".join(s['text'] for s in self.last_sections if s['text'].strip())
        return self.last_text

    def get_memory(self) -> ShortTermMemory:
        return self._memory
