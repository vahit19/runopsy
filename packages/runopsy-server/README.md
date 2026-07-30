# runopsy-server

The local API from section 19.2, plus a plain index page.

Local means local. It binds to loopback, has no authentication, and is not built to be
exposed — everything it serves is the contents of somebody's private repository, and an
API that could be published would need a different security posture than one that
cannot reach the network.

Two rules are enforced rather than documented:

**Read-mostly.** Ingesting events is allowed, because an adapter may prefer HTTP to a
subprocess. Replay *execution* is not exposed at all. The only thing in Runopsy that can
change the world stays behind a command a person types, where the plan can be read
first — a socket is a poor place to make that decision on someone's behalf.

**The same redaction as everywhere else.** Reports served here are redacted exactly as
`runopsy export` redacts them, and observed edges are returned separately from inferred
ones so a viewer cannot draw a guess and a measurement as the same kind of line. A
second surface with weaker rules is how the first surface's rules stop mattering.
