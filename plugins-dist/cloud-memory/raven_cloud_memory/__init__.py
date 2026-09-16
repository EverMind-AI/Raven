"""Hosted memory services as Raven memory backends.

Three thin clients over one shape (:class:`raven_cloud_memory._base.CloudBackend`):
Mem0 Platform, Zep Cloud and MemOS Cloud. Each is a set of REST calls behind
an API key; none runs a process, keeps local storage or reads the host's
embedding endpoint.
"""
