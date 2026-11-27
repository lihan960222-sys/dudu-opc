"""Bind app-side MCP permissions to calls observed on one native Codex turn."""

import asyncio
import json
from uuid import uuid4

from app.services.agent_memory import redact_json, redact_secrets
from app.services.local_approval_broker import MAX_REVIEWABLE_INPUT_CHARS
from app.services.provider_adapter import ALLOW


class ProjectMcpApprovals:
    """The mailbox and Codex notifications travel independently. Only the active,
    authenticated tool item can originate an application approval. The bridge
    consumes the resulting database receipt once, before contacting the service.
    """

    def __init__(self, gate, clock):
        self.gate = gate
        self.clock = clock
        self.items = {}
        self.claims = {}
        self.receipts = {}
        self.changed = asyncio.Event()
        self.closed = False

    @staticmethod
    def _key(arguments):
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def started(self, item_id, tool, arguments, summary):
        if self.closed or item_id in self.items or not isinstance(arguments, dict):
            return
        key = self._key(arguments)
        if (len(key) > MAX_REVIEWABLE_INPUT_CHARS or redact_json(arguments) != arguments
                or redact_secrets(key)[1]):
            return
        self.items[item_id] = (tool, key, summary)
        self.changed.set()

    async def claim(self, tool, arguments):
        """Return the receipt ID for this one dispatch, or None without executing."""
        key = self._key(arguments)
        try:
            async with asyncio.timeout(5):
                while True:
                    if self.closed:
                        return None
                    item_id = next((i for i, (name, data, _) in self.items.items()
                                    if name == tool and data == key and i not in self.claims), None)
                    if item_id is not None:
                        break
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError:
            return None

        request_id = f"codex-project-app-{uuid4().hex}"
        self.claims[item_id] = request_id
        summary = self.items[item_id][2]
        self.clock.pause()
        try:
            handle = await self.gate.open(request_id=request_id, tool=tool, input=arguments,
                                          summary=summary, kind="other")
            if handle is None:
                return None
            handle.tool = tool
            if self.claims.get(item_id) != request_id:
                await self.gate.cancel(request_id)
            decision = await self.gate.decide(handle)
            if decision.kind != ALLOW:
                return None
            live = self.claims.get(item_id) == request_id
            if live:
                self.receipts[item_id] = handle.approval_id
            await self.gate.native_response(handle.approval_id, live)
            return handle.approval_id if live else None
        finally:
            self.clock.resume()
            await self.gate.cancel(request_id)

    async def finished(self, item_id):
        self.items.pop(item_id, None)
        request_id = self.claims.pop(item_id, None)
        if request_id:
            await self.gate.cancel(request_id)
        receipt = self.receipts.pop(item_id, None)
        if receipt:
            # An MCP timeout can close the native item while the bridge still
            # checks its remote grant. Retire any receipt it has not consumed.
            await self.gate.native_response(receipt, False)

    async def close(self):
        self.closed = True
        self.changed.set()
        for item_id in tuple(self.items):
            await self.finished(item_id)
