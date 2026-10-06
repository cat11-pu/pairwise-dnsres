# dnsres

A dependency free recursive resolver kernel: an answer cache with TTLs, CNAME
chain chasing, negative answers, truncated answer retries and batch lookups.

The package never touches the network. The upstream server is a table of
canned answers handed in by the caller, and every question that reaches it is
appended to its wire journal, so a resolution can be replayed step by step from
a test. Time comes from an injectable clock; no sockets, threads or third party
packages are involved.

## Layout

    dnsres/__init__.py     public names re-exported by the package
    dnsres/core.py         cache, TTL handling, CNAME chasing, coalescing
    tests/__init__.py      test package marker
    tests/test_core.py     behavioural test suite

## Running the tests

From the project root:

    python3 -m unittest discover -s tests -v

Only the Python standard library is required; there is nothing to install.
