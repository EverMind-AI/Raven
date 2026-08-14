"""rpc package -- the JSON-RPC protocol between Runtime and any interactive client.

Single source of truth for the contract lives in
``rpc-schema/openrpc.json``.  The Pydantic v2 models in
:mod:`raven.rpc.models` are hand-written counterparts kept in sync via
``tests/test_rpc_schema_match.py``.
"""
