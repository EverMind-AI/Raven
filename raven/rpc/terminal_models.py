"""Request and response models for hosted terminals and runtime discovery."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from raven.contracts.terminal import RuntimeInfo, SendResult, TerminalListResult, TerminalRecord, WaitResult


class TerminalParams(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RuntimeStatusParams(TerminalParams):
    pass


class RuntimeStatusInfo(RuntimeInfo):
    state: Literal["ready"]
    environment: str
    app_version: str = Field(alias="appVersion")
    capabilities: list[str]


class RuntimeGraphInfo(TerminalParams):
    state: Literal["ready"]


class RuntimeStatusResult(TerminalParams):
    runtime: RuntimeStatusInfo
    graph: RuntimeGraphInfo


class TerminalCreateParams(TerminalParams):
    worktree_id: str
    command: str | list[str]
    title: str = "Terminal"
    owner: str = "human"
    session_id: str | None = None


class TerminalShowParams(TerminalParams):
    handle: str


class TerminalRecordResult(TerminalParams):
    terminal: TerminalRecord


class TerminalListParams(TerminalParams):
    worktree_id: str | None = None
    limit: int = Field(default=1000, ge=1, le=1_000_000)
    include_visual_layouts: bool = False


class TerminalSendParams(TerminalParams):
    handle: str | None = None
    to: Literal["raven"] | None = None
    text: str
    enter: bool = False
    require_ack: bool = False
    force: bool = False
    source_handle: str | None = None
    session_id: str | None = None


class TerminalDeliveryResult(SendResult):
    handle: str | None = None
    state: Literal["accepted", "queued", "blocked", "stalled", "delivered_to_host"]
    to: Literal["raven"] | None = None


class TerminalSendResult(TerminalParams):
    send: TerminalDeliveryResult


class TerminalWaitParams(TerminalShowParams):
    for_condition: Literal["tui-idle", "exit"] = Field(default="tui-idle", alias="for")
    timeout_ms: int = Field(default=300000, ge=1, le=3_600_000)


class TerminalWaitResult(TerminalParams):
    wait: WaitResult


class TerminalClosed(TerminalParams):
    handle: str
    closed: bool


class TerminalCloseResult(TerminalParams):
    close: TerminalClosed


class TerminalInputParams(TerminalShowParams):
    data: str


class TerminalInput(TerminalParams):
    handle: str
    bytes_written: int = Field(alias="bytesWritten")


class TerminalInputResult(TerminalParams):
    input: TerminalInput


class TerminalSubscribeParams(TerminalShowParams):
    enabled: bool = True
    ack: int | None = Field(default=None, ge=0, le=2**53 - 1)


class TerminalSubscription(TerminalParams):
    handle: str
    enabled: bool | None = None
    seq: int | None = None
    ack: int | None = None
    ack_bytes: int | None = Field(default=None, alias="ackBytes")
    subscription_id: str | None = None


class TerminalSubscribeResult(TerminalParams):
    subscription: TerminalSubscription


class TerminalRenameParams(TerminalShowParams):
    title: str


class TerminalRename(TerminalParams):
    handle: str
    tab_id: str = Field(alias="tabId")
    title: str
    live_title: str = Field(alias="liveTitle")


class TerminalRenameResult(TerminalParams):
    rename: TerminalRename


TERMINAL_METHOD_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "runtime.status": (RuntimeStatusParams, RuntimeStatusResult),
    "terminal.create": (TerminalCreateParams, TerminalRecordResult),
    "terminal.list": (TerminalListParams, TerminalListResult),
    "terminal.show": (TerminalShowParams, TerminalRecordResult),
    "terminal.send": (TerminalSendParams, TerminalSendResult),
    "terminal.wait": (TerminalWaitParams, TerminalWaitResult),
    "terminal.close": (TerminalShowParams, TerminalCloseResult),
    "terminal.input": (TerminalInputParams, TerminalInputResult),
    "terminal.subscribe": (TerminalSubscribeParams, TerminalSubscribeResult),
    "terminal.rename": (TerminalRenameParams, TerminalRenameResult),
}
