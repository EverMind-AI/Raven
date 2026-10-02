"""Bind native mailbox tools to a current session receiver, without host authority."""

from uuid import uuid4

from raven.agent.tools.mailbox import native_mailbox_tools
from raven.contracts.mailbox import MailboxError
from raven.rpc import connection


def register_mailbox_tools(tools, dispatcher):
    async def rpc(method, params, session_key):
        methods = dispatcher.mailbox_receivers
        binding = methods.receivers.for_session(session_key)
        if binding.terminal_handle is not None or binding.session_key != session_key:
            raise MailboxError("receiver_unauthorized")
        token = connection.bind_connection(mailbox_binding_id=binding.binding_id)
        try:
            frame = await dispatcher.dispatch(
                {"jsonrpc": "2.0", "id": str(uuid4()), "method": method, "params": params}
            )
        finally:
            connection.unbind_connection(token)
        if "error" in frame:
            error = frame["error"]
            data = error.get("data") or {}
            raise MailboxError(data.get("code", "receiver_unauthorized"), retryable=data.get("retryable", False))
        return frame["result"]["data"]

    installed = native_mailbox_tools(rpc)
    for tool in installed:
        tools.register(tool)

    def unregister():
        for tool in installed:
            if tools.get(tool.name) is tool:
                tools.unregister(tool.name)

    return unregister
