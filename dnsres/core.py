"""A miniature recursive resolver: cache, TTL, CNAME chains, negative answers.

The module never touches the network.  The upstream server is a table of
canned answers handed in by the caller, and every question that reaches it is
recorded in its wire journal, so a whole resolution - a truncated answer
retried over TCP, a CNAME chain walked to its end, two callers asking the same
question at the same time - can be replayed deterministically in one process.
Time comes from an injected clock.

Vocabulary used throughout:

* ``name``      the name a question is about
* ``rtype``     the record type of the question, e.g. ``A``
* ``ttl``       seconds an answer may stay in the cache
* ``entry``     one cached answer: the records it holds, the instant it was
                stored and the instant it stops being usable; a record served
                from an entry reports the time it has left, so callers see the
                TTL count down while the entry is alive.
"""

from __future__ import annotations

A = "A"
AAAA = "AAAA"
CNAME = "CNAME"

NOERROR = "NOERROR"
NXDOMAIN = "NXDOMAIN"

UDP = "udp"
TCP = "tcp"


# --------------------------------------------------------------------------- errors


class ResolutionError(Exception):
    """A question that cannot be answered at all."""


class CnameLoop(ResolutionError):
    """The CNAME chain came back to a name it had already visited."""


class ChainTooDeep(ResolutionError):
    """The CNAME chain is longer than the resolver is allowed to follow."""


# --------------------------------------------------------------------------- names


def canonical(name):
    """Normalise a domain name so that it can be used as a lookup key."""
    return str(name).strip().rstrip(".").lower()


# --------------------------------------------------------------------------- time


class Clock:
    """A manual clock; the caller decides how fast time moves."""

    def __init__(self, now=0.0):
        self._now = float(now)

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += float(seconds)
        return self._now

    def __repr__(self):
        return "Clock(now=%r)" % (self._now,)


# --------------------------------------------------------------------------- answers


class Record:
    """One resource record: owner name, type, time to live and payload."""

    __slots__ = ("name", "rtype", "ttl", "data")

    def __init__(self, name, rtype, ttl, data):
        self.name = str(name)
        self.rtype = str(rtype).upper()
        self.ttl = float(ttl)
        self.data = data

    def with_ttl(self, ttl):
        """The same record carrying a different time to live."""
        return Record(self.name, self.rtype, ttl, self.data)

    def __eq__(self, other):
        if not isinstance(other, Record):
            return NotImplemented
        return (self.name, self.rtype, self.ttl, self.data) == (
            other.name,
            other.rtype,
            other.ttl,
            other.data,
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.name, self.rtype, self.ttl, self.data))

    def __repr__(self):
        return "Record(%r, %r, %r, %r)" % (self.name, self.rtype, self.ttl, self.data)


class Reply:
    """An answer as it arrives from the upstream server."""

    __slots__ = ("records", "rcode", "truncated")

    def __init__(self, records=(), rcode=NOERROR, truncated=False):
        self.records = list(records)
        self.rcode = str(rcode)
        self.truncated = bool(truncated)

    def __repr__(self):
        return "Reply(records=%r, rcode=%r, truncated=%r)" % (
            self.records,
            self.rcode,
            self.truncated,
        )


class Answer:
    """The outcome of one question: code, records and where they came from."""

    __slots__ = ("name", "rtype", "rcode", "records", "cached")

    def __init__(self, name, rtype, rcode=NOERROR, records=(), cached=False):
        self.name = name
        self.rtype = rtype
        self.rcode = rcode
        self.records = list(records)
        self.cached = bool(cached)

    def __repr__(self):
        return "Answer(%r, %r, rcode=%r, records=%r, cached=%r)" % (
            self.name,
            self.rtype,
            self.rcode,
            self.records,
            self.cached,
        )


# --------------------------------------------------------------------------- upstream


class Upstream:
    """A canned upstream server.

    ``table`` maps a question to the reply the server sends back.  Questions
    named in ``truncated`` come back over UDP with the truncation bit set and
    only their leading record, while the whole table entry is served over TCP.
    Every question that reaches the server is appended to ``wire``, so a test
    can tell how many exchanges a resolution really cost.
    """

    def __init__(self, table=None, truncated=()):
        self.table = {}
        for (name, rtype), entry in (table or {}).items():
            self.table[self._key(name, rtype)] = entry
        self.truncated = {self._key(name, rtype) for name, rtype in truncated}
        self.wire = []

    @staticmethod
    def _key(name, rtype):
        """The server answers a question however it happens to be spelled."""
        return (str(name).strip().rstrip(".").lower(), str(rtype).upper())

    def ask(self, name, rtype, transport=UDP):
        key = self._key(name, rtype)
        self.wire.append((key[0], key[1], transport))
        entry = self.table.get(key)
        if entry is None:
            return Reply((), NXDOMAIN)
        if transport == UDP and key in self.truncated:
            return Reply(entry.records[:1], entry.rcode, True)
        return Reply(entry.records, entry.rcode, entry.truncated)


class CacheEntry:
    """A cached answer and the instants that bracket its life."""

    __slots__ = ("rcode", "records", "stored_at", "expires_at")

    def __init__(self, rcode, records, stored_at, expires_at):
        self.rcode = rcode
        self.records = list(records)
        self.stored_at = stored_at
        self.expires_at = expires_at


# --------------------------------------------------------------------------- resolver


class ResolverCore:
    """A cache in front of a canned upstream server."""

    def __init__(self, upstream, clock, negative_ttl=60.0, max_cname_depth=8):
        self.upstream = upstream
        self.clock = clock
        self.negative_ttl = float(negative_ttl)
        self.max_cname_depth = int(max_cname_depth)
        self.cache = {}

    # ------------------------------------------------------------------ public

    def resolve(self, qname, qtype=A):
        """Answer a single question."""
        return self.resolve_many([(qname, qtype)])[0]

    def resolve_many(self, questions):
        """Answer several questions.

        Questions asked together share the work: a question that repeats one
        already answered in the same batch costs no further exchange.
        """
        shared = {}
        return [self._question(qname, qtype, shared) for qname, qtype in questions]

    def _question(self, qname, rtype, shared):
        name = canonical(qname)
        rtype = str(rtype).upper()
        key = (name, rtype)
        if key in shared:
            return shared[key]
        answer = self._answer(key, 0, frozenset())
        shared[key] = answer
        return answer

    # ------------------------------------------------------------------ cache

    def _answer(self, key, depth, seen):
        """Answer a question from the cache when the entry is still usable."""
        now = self.clock.now()
        entry = self.cache.get(key)
        if entry is not None and now < entry.expires_at:
            elapsed = now - entry.stored_at
            records = [record.with_ttl(record.ttl - elapsed) for record in entry.records]
            return Answer(key[0], key[1], entry.rcode, records, True)
        return self._fetch(key, depth, seen)

    def _fetch(self, key, depth, seen):
        name, rtype = key
        if name in seen:
            raise CnameLoop("cname loop at %r" % (name,))
        if depth > self.max_cname_depth:
            raise ChainTooDeep("cname chain deeper than %d" % (self.max_cname_depth,))
        reply = self._exchange(name, rtype)
        now = self.clock.now()
        if reply.rcode == NXDOMAIN:
            self._store(key, NXDOMAIN, [], self.negative_ttl, now)
            return Answer(name, rtype, NXDOMAIN, [], False)
        records = list(reply.records)
        target = self._cname_target(name, records)
        if target is not None and rtype != CNAME:
            tail = self._answer((canonical(target), rtype), depth + 1, seen | {name})
            records.extend(tail.records)
        ttl = min(record.ttl for record in records) if records else self.negative_ttl
        self._store(key, NOERROR, records, ttl, now)
        return Answer(name, rtype, NOERROR, records, False)

    def _exchange(self, name, rtype):
        """Ask the upstream, retrying over TCP when the answer is truncated."""
        reply = self.upstream.ask(name, rtype, UDP)
        if reply.truncated:
            reply = self.upstream.ask(name, rtype, TCP)
        return reply

    @staticmethod
    def _cname_target(name, records):
        """The name a leading CNAME in these records points at, if any."""
        for record in records:
            if record.rtype == CNAME and canonical(record.name) == name:
                return record.data
        return None

    def _store(self, key, rcode, records, ttl, now):
        self.cache[key] = CacheEntry(rcode, records, now, now + ttl)
