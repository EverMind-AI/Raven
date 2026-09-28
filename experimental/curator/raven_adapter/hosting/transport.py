"""Compose native ACP components around an experiment-owned runtime and private inspection."""

import asyncio

from raven.acp.methods import AcpMethods
from raven.acp.outbound import OutboundRequests
from raven.acp.permissions import AcpPermissionBroker
from raven.acp.questions import AcpQuestions
from raven.acp.server import ACP_CHANNEL, _answer, _drain
from raven.acp.stdio import read_frames, write_frame
from raven.acp.updates import UpdateTranslator
from raven.permissions.shell_policy import declare_default_families


class HostMethods(AcpMethods):
    """Add fixed host operations after the native initialization and frame checks."""

    def __init__(self, *, extensions, **kwargs):
        super().__init__(**kwargs)
        self.extensions = dict(extensions)
        if any(not name.startswith("_") for name in self.extensions):
            raise ValueError("host methods must use an underscore-prefixed namespace")

    async def _route(self, method, params, *, is_request):
        if self.initialized and method in self.extensions:
            return await self.extensions[method](params)
        return await super()._route(method, params, is_request=is_request)


async def serve(reader, out, *, stack_factory, extensions):
    """Own startup and teardown; native components retain sessions, permissions and cancellation."""

    def emit(frame):
        write_frame(out, frame)

    outbound = OutboundRequests(emit=emit)
    translator = UpdateTranslator(emit=emit, side_channel=lambda method, params: questions.handle(method, params))
    questions = AcpQuestions(outbound=outbound, translator=translator, emit=emit)
    permissions = AcpPermissionBroker(outbound=outbound, translator=translator)
    declare_default_families()
    stack, methods = None, None
    tasks = set()
    try:
        stack = await stack_factory(translator, channel=ACP_CHANNEL, approval_responder=permissions)
        questions.set_broker(stack.question_broker)
        methods = HostMethods(
            dispatcher=stack.dispatcher,
            translator=translator,
            emit=emit,
            agent_loop=stack.agent_loop,
            outbound=outbound,
            questions=questions,
            channel=ACP_CHANNEL,
            extensions=extensions,
        )
        async for frame in read_frames(reader, emit):
            task = asyncio.create_task(_answer(methods, frame, emit))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        try:
            await questions.drain()
        finally:
            outbound.close()
            try:
                await _drain(translator, tasks)
            finally:
                try:
                    if methods is not None:
                        await methods.unsubscribe_all()
                finally:
                    if stack is not None:
                        await stack.teardown()
