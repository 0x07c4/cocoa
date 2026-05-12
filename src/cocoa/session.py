from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import config as cocoa_config
from .models import ItemRecord, ThreadRecord
from .projection import ThreadView, load_thread_view
from .providers import (
    ProviderConfigurationError,
    StubProvider,
    is_provider_configured,
    provider_from_env,
    resolve_provider_model,
    resolve_provider_status,
    resolve_routed_provider_status,
)
from .runtime import AgentRuntime, UserTurnResult
from .store import JsonlStore
from .tools import (
    BUILTIN_TOOLS,
    CommandResult,
    ConsoleApprovalPrompter,
    ShellTool,
    ToolDescriptor,
)


class SessionEngine:
    """Owns one active conversation session: runtime, thread, overrides, and shell.

    `cli.py` should call this layer instead of managing runtime/thread/overrides
    directly. Lifetime matches one REPL session or one ``ask`` command.
    """

    def __init__(self, cwd: Path, overrides: Mapping[str, str] | None = None) -> None:
        self.cwd = cwd
        self._overrides: dict[str, str] = dict(overrides) if overrides else {}
        self._runtime: AgentRuntime
        self._store: JsonlStore
        self._thread: ThreadRecord
        self._provider_status: str = ""
        self._shell: ShellTool = ShellTool(ConsoleApprovalPrompter())
        self._rebuild_runtime()

    @property
    def overrides(self) -> dict[str, str]:
        return dict(self._overrides)

    @property
    def runtime(self) -> AgentRuntime:
        return self._runtime

    @property
    def store(self) -> JsonlStore:
        return self._store

    @property
    def thread(self) -> ThreadRecord:
        return self._thread

    @property
    def provider_status(self) -> str:
        return self._provider_status

    @property
    def shell(self) -> ShellTool:
        return self._shell

    def set_thread(self, thread: ThreadRecord) -> None:
        self._thread = thread

    def _effective_session_environment(self) -> dict[str, str]:
        config = cocoa_config.resolve_config(self.cwd, self._overrides)
        mode = cocoa_config.resolve_model_mode(config)
        return cocoa_config.to_env_mapping(config, mode)

    def _provider_environment(self) -> tuple[dict[str, str], bool]:
        env = self._effective_session_environment()
        return cocoa_config.provider_environment_for_mode(env)

    def _rebuild_runtime(self) -> None:
        env = self._effective_session_environment()
        provider_env, _ = self._provider_environment()
        self._store = JsonlStore.for_workspace(self.cwd)
        config = cocoa_config.resolve_config(self.cwd, self._overrides)
        budget = config.get("budget", {}) if isinstance(config.get("budget"), dict) else {}
        self._provider_status = resolve_provider_status(provider_env)
        try:
            provider = provider_from_env(provider_env)
        except ProviderConfigurationError:
            provider = StubProvider()
        self._runtime = AgentRuntime(
            store=self._store, provider=provider, budget=budget,
        )

    def set_override(self, key: str, value: str) -> None:
        self._overrides[key] = value
        self._rebuild_runtime()

    def clear_overrides(self) -> None:
        self._overrides.clear()
        self._rebuild_runtime()

    def current_env(self) -> dict[str, str]:
        return self._effective_session_environment()

    def resolved_mode(self) -> str:
        return cocoa_config.resolve_model_mode_from_env(self.current_env())

    def routing_payload(self) -> dict[str, Any]:
        return cocoa_config.config_to_routing_payload(self.cwd, self._overrides)

    def is_provider_configured(self) -> bool:
        return is_provider_configured(self.current_env())

    def provider_status_text(self) -> str:
        return resolve_routed_provider_status(self.current_env())

    def provider_model(self) -> str:
        return resolve_provider_model(self.current_env())

    def start_thread(self, title: str | None = None) -> ThreadRecord:
        self._thread = self._runtime.start_thread(self.cwd, title=title)
        return self._thread

    def resume_thread(self, thread_id: str) -> ThreadRecord:
        self._thread = self._runtime.resume_thread(thread_id)
        return self._thread

    def load_view(self) -> ThreadView:
        return load_thread_view(self._store, self._thread.id)

    def thread_path(self) -> str:
        return str(self._store.thread_path(self._thread.id))

    async def run_user_turn(self, prompt: str) -> str:
        result = await self.run_user_turn_with_result(prompt)
        return result.message

    async def run_user_turn_with_result(self, prompt: str) -> UserTurnResult:
        return await self._runtime.run_user_turn_with_result(
            self._thread,
            prompt,
            routing=self.routing_payload(),
        )

    async def run_shell_turn(self, command: str) -> CommandResult:
        return await self._runtime.run_shell_turn(self._thread, command, self._shell)

    async def run_proposed_command(self, item_id: str) -> CommandResult:
        return await self._runtime.run_proposed_command(
            self._thread, item_id, self._shell,
        )

    def apply_proposed_file_write(self, item_id: str) -> ItemRecord:
        return self._runtime.apply_proposed_file_write(self._thread, item_id)

    def reject_pending_item(self, item_id: str) -> ItemRecord:
        return self._runtime.reject_pending_item(self._thread, item_id)

    async def run_escalation_turn(self) -> UserTurnResult:
        return await self._runtime.run_escalation_turn(
            self._thread,
            target="last",
            routing=self.routing_payload(),
        )

    def create_task(
        self,
        subject: str,
        description: str = "",
        *,
        owner: str | None = None,
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ItemRecord:
        return self._runtime.create_task(
            self._thread, subject, description,
            owner=owner, blocks=blocks, blocked_by=blocked_by, metadata=metadata,
        )

    def update_task(
        self,
        item_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ItemRecord:
        return self._runtime.update_task(
            self._thread, item_id,
            status=status, owner=owner, blocks=blocks,
            blocked_by=blocked_by, metadata=metadata,
        )

    def list_tasks(self) -> tuple[ItemRecord, ...]:
        return self._runtime.list_tasks(self._thread)

    def list_registered_tools(self) -> tuple[ToolDescriptor, ...]:
        return BUILTIN_TOOLS
