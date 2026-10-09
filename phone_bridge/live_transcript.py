"""Bounded display-only drafts; recordings retain authoritative final transcripts."""


class LiveTranscript:
    def __init__(self):
        self.entries = {}
        self.interrupted = {}

    def update(self, role, item, text, seconds, *, delta=False, final=False):
        if role not in ('remote', 'agent_generated') or not isinstance(item, str) or not item or not isinstance(text, str):
            return
        identity = (role, item)
        entry = self.entries.get(identity)
        if entry and entry['final'] and not final:
            return
        if not entry:
            entry = {'role':role, 'item_id':item, 'text':'', 'seconds':round(seconds, 2),
                     'final':False, 'interrupted':role == 'agent_generated' and item in self.interrupted,
                     'truncated':False}
            self.entries[identity] = entry
        value = entry['text'] + text if delta else text
        entry.update(text=value[:2000], final=final,
                     truncated=len(value) > 2000 or delta and entry['truncated'])
        while len(self.entries) > 60:
            del self.entries[next(iter(self.entries))]

    def interrupt(self, item):
        self.interrupted[item] = True
        while len(self.interrupted) > 60:
            del self.interrupted[next(iter(self.interrupted))]
        entry = self.entries.get(('agent_generated', item))
        if entry:
            entry['interrupted'] = True

    def snapshot(self):
        return [dict(entry) for entry in self.entries.values()]
