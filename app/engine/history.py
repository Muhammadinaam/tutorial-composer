from __future__ import annotations


class EditHistory:
    """Snapshots taken before an edit, so Undo can put that state back."""

    def __init__(self, limit: int = 40):
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self.limit = limit
        self.pending: dict | None = None

    def begin(self, snapshot: dict) -> None:
        if self.pending is None:
            self.pending = snapshot

    def cancel(self) -> None:
        self.pending = None

    def commit(self, snapshot: dict) -> bool:
        before = self.pending
        self.pending = None
        return self.push(before, snapshot)

    def push(self, before: dict | None, after: dict) -> bool:
        if before is None or before == after:
            return False
        self.undo_stack.append(before)
        if len(self.undo_stack) > self.limit:
            del self.undo_stack[0]
        self.redo_stack.clear()
        return True

    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def undo(self, current: dict) -> dict | None:
        if not self.undo_stack:
            return None
        self.redo_stack.append(current)
        return self.undo_stack.pop()

    def redo(self, current: dict) -> dict | None:
        if not self.redo_stack:
            return None
        self.undo_stack.append(current)
        return self.redo_stack.pop()
