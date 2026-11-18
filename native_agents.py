# SPDX-License-Identifier: Apache-2.0
"""Shared read-only native discovery; relay jobs are passed in by the caller."""
from delegate_config import CONFIG
import claude_agents
import codex_agents


class NativeAgents:
    def __init__(self, relay_sessions=None, stall_seconds=None):
        self.readers = []
        if CONFIG['claude_agents_enabled']:
            self.readers.append(claude_agents.ClaudeAgents(stall_seconds=stall_seconds))
        if CONFIG.get('codex_agents_enabled', True):
            self.readers.append(codex_agents.CodexAgents(relay_sessions=relay_sessions, stall_seconds=stall_seconds))

    def scan(self, now=None):
        records = []
        for reader in self.readers:
            records.extend(reader.scan() if now is None else reader.scan(now))
        return records
