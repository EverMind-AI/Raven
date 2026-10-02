"""Real process handoff proposal and concurrent host ownership transfer checks."""

import multiprocessing
from uuid import uuid4

from raven.contracts.mailbox import MailboxError
from raven.mailbox.handoff import MailboxHandoff
from raven.mailbox.store import MailboxStore
from tests.test_mailbox_handoff import BINDING, acceptance
from tests.test_mailbox_handoff import handoff as handoff
from tests.test_mailbox_store import NOW, SCOPE

CTX = multiprocessing.get_context("spawn")


def receiver_process(root, sender, receiver, offer, sha, output):
    store = MailboxStore(root)
    service = MailboxHandoff(store)
    content = service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    output.put((content, service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)))


def host_process(root, params, barrier, output):
    service = MailboxHandoff(MailboxStore(root))
    barrier.wait(timeout=20)
    try:
        output.put(("committed", service.commit(**params, revalidate_receiver=lambda: None, now=NOW)))
    except MailboxError as error:
        output.put((error.code, None))


def test_receiver_process_proposal_then_two_host_commits_transfer_once(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    output = CTX.Queue()
    reader = CTX.Process(target=receiver_process, args=(store.root, sender, receiver, offer, sha, output))
    hosts = []
    try:
        reader.start()
        content, proposal = output.get(timeout=20)
        reader.join(timeout=5)
        assert reader.exitcode == 0
        assert content == b"complete authorized snapshot"
        assert proposal["status"] == "PROPOSED"
        assert service.task_status(**SCOPE)["owner_agent_id"] == sender.agent_id
        params = dict(
            binding_id=BINDING,
            receiver_ref=receiver,
            scope=SCOPE,
            offer_message_id=offer.message_id,
            accept_message_id=proposal["accept_message_id"],
            expected_owner_agent_id=sender.agent_id,
            expected_assignment_epoch=1,
        )
        barrier = CTX.Barrier(2)
        requests = [str(uuid4()), str(uuid4())]
        hosts = [
            CTX.Process(target=host_process, args=(store.root, dict(params, request_id=request), barrier, output))
            for request in requests
        ]
        for host in hosts:
            host.start()
        values = [output.get(timeout=20) for _ in hosts]
        for host in hosts:
            host.join(timeout=5)
            assert host.exitcode == 0
        assert sorted(value[0] for value in values) == ["assignment_conflict", "committed"]
        confirmed = service.task_status(**SCOPE)
        assert confirmed["owner_agent_id"] == receiver.agent_id
        assert confirmed["assignment_epoch"] == 2
        assert confirmed["accept_message_id"] == proposal["accept_message_id"]
        with store.db.connection() as conn:
            assert (
                conn.execute("SELECT count(*) FROM mutation_receipts WHERE method='handoff.commit'").fetchone()[0] == 1
            )
    finally:
        for child in [reader, *hosts]:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)
        output.close()
        output.join_thread()
