# Personal robot registrations

The Connections hub separates **Servers**, where Raven may run an agent through
its existing SSH/node support, from **Robots**, which are device records without
an execution node. Existing machine IDs, configuration, SSH credentials, CLI
commands, and `machines.*` methods are unchanged; only their displayed label is
now Servers.

The Robots tab supports adding, searching, filtering, editing, moving records to
trash, and restoring them. Registration does not establish a connection.
Company devices and credentials are out of scope: the form blocks company
registration, and the runtime accepts only explicitly confirmed personal records.
Use independently governed instances for other security domains rather than
mixing their data into the personal registry.

## Optional read-only status

SparkBot and generic HTTP devices can have a status URL. Robot dogs, AGVs, mobile
manipulators, and other device types support metadata only. No robot is treated
as a server and no executable node is installed.

Only **Check status** contacts a device. It sends one GET, without credentials,
proxies, or redirects, to a literal RFC1918 IPv4 address whose path is `/health`
or `/status`. Ports are limited to 80, 443, or 1024-65535. DNS names, public,
loopback, and link-local addresses, credentials, query parameters, and fragments
are rejected. The timeout is three seconds and the uncompressed JSON response limit is 16 KiB.
Compressed responses and non-standard numeric constants are rejected.
An optional expected `project` value prevents accepting a different device.
Only a bounded allowlist of health fields is retained.

The states are unknown, not connected, error, and status endpoint reachable.
`ok: true` confirms the status endpoint only: it does not establish chassis,
audio, or motion acceptance. Results expire after 60 seconds, including when a
page remains open. Failed and malformed responses are never shown as online.
There is no scanning or background device polling and no control, shell, ROS,
MQTT, motion, or media endpoint.

## Persistence and compatibility

The existing authenticated RPC transport serves `robots.list`, `robots.save`,
`robots.remove`, `robots.restore`, and `robots.check`. Web clients use the same
typed transport and source seam as the other hub tabs. An older gateway without
these methods produces an unavailable message rather than invented records.
Offline fixtures never contact a real device or claim it is connected.

Records live under `robots/<config-stem>/personal/devices.sqlite3` beside the
resolved configuration file. Separate configuration files have separate stores;
the default is therefore under the instance's Raven home. The directory is
owner-only and the database is owner-readable/writable on POSIX. Records are
limited to 100, including trash. Removal is recoverable; restoring or editing a
record clears its old health observation. Revisions prevent a stale editor or
an overlapping health request from overwriting a later edit or removal.

The feature does not modify the server registry or model/provider configuration.
Reverting the code leaves the separate database intact and stops exposing the
tab and RPC methods. Reapplying the feature reopens those registrations.
