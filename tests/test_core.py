"""Behaviour tests for the resolver core."""

import unittest

from dnsres.core import (
    A,
    AAAA,
    CNAME,
    NOERROR,
    NXDOMAIN,
    Clock,
    CnameLoop,
    Record,
    Reply,
    ResolutionError,
    ResolverCore,
    Upstream,
)

ADDRESS = "93.184.216.34"


def address(name, value=ADDRESS, ttl=300.0):
    return Record(name, A, ttl, value)


def alias(name, target, ttl=300.0):
    return Record(name, CNAME, ttl, target)


def reply(*records, rcode=NOERROR):
    return Reply(list(records), rcode)


def build(table=None, truncated=(), negative_ttl=60.0, max_cname_depth=8):
    clock = Clock()
    upstream = Upstream(table or {}, truncated)
    resolver = ResolverCore(
        upstream,
        clock,
        negative_ttl=negative_ttl,
        max_cname_depth=max_cname_depth,
    )
    return resolver, upstream, clock


class LookupTests(unittest.TestCase):
    def test_first_question_goes_upstream_and_the_second_is_served_from_cache(self):
        resolver, upstream, clock = build(
            {("www.example.com", A): reply(address("www.example.com"))}
        )

        first = resolver.resolve("www.example.com")

        self.assertEqual(first.rcode, NOERROR)
        self.assertEqual(first.records, [address("www.example.com")])
        self.assertFalse(first.cached)
        self.assertEqual(upstream.wire, [("www.example.com", A, "udp")])

        second = resolver.resolve("www.example.com")

        self.assertTrue(second.cached)
        self.assertEqual(second.records, [address("www.example.com")])
        self.assertEqual(upstream.wire, [("www.example.com", A, "udp")])

    def test_cached_ttl_counts_down_and_the_entry_expires(self):
        resolver, upstream, clock = build(
            {("www.example.com", A): reply(address("www.example.com", ttl=300.0))}
        )
        resolver.resolve("www.example.com")

        clock.advance(100.0)
        alive = resolver.resolve("www.example.com")

        self.assertTrue(alive.cached)
        self.assertEqual([record.ttl for record in alive.records], [200.0])
        self.assertEqual(len(upstream.wire), 1)

        clock.advance(200.0)
        expired = resolver.resolve("www.example.com")

        self.assertFalse(expired.cached)
        self.assertEqual([record.ttl for record in expired.records], [300.0])
        self.assertEqual(len(upstream.wire), 2)

    def test_missing_name_is_remembered_for_the_negative_ttl(self):
        resolver, upstream, clock = build(
            {("www.example.com", A): reply(address("www.example.com"))}
        )

        missing = resolver.resolve("nowhere.example.com")

        self.assertEqual(missing.rcode, NXDOMAIN)
        self.assertEqual(missing.records, [])
        self.assertEqual(len(upstream.wire), 1)

        clock.advance(10.0)
        again = resolver.resolve("nowhere.example.com")

        self.assertEqual(again.rcode, NXDOMAIN)
        self.assertTrue(again.cached)
        self.assertEqual(len(upstream.wire), 1)

        clock.advance(60.0)
        later = resolver.resolve("nowhere.example.com")

        self.assertEqual(later.rcode, NXDOMAIN)
        self.assertFalse(later.cached)
        self.assertEqual(len(upstream.wire), 2)

    def test_the_spelling_of_a_question_does_not_change_the_cache_key(self):
        resolver, upstream, clock = build(
            {("www.example.com", A): reply(address("www.example.com"))}
        )

        loud = resolver.resolve("WWW.Example.COM.")

        self.assertEqual(loud.name, "www.example.com")
        self.assertEqual(loud.records, [address("www.example.com")])

        quiet = resolver.resolve("www.example.com")

        self.assertTrue(quiet.cached)
        self.assertEqual(quiet.records, [address("www.example.com")])
        self.assertEqual(len(upstream.wire), 1)


class ChainTests(unittest.TestCase):
    def test_a_cname_chain_is_followed_to_the_end(self):
        resolver, upstream, clock = build(
            {
                ("www.example.com", A): reply(
                    alias("www.example.com", "cdn.example.net", ttl=100.0)
                ),
                ("cdn.example.net", A): reply(
                    address("cdn.example.net", "203.0.113.7", ttl=300.0)
                ),
            }
        )

        first = resolver.resolve("www.example.com")

        self.assertEqual(
            first.records,
            [
                alias("www.example.com", "cdn.example.net", ttl=100.0),
                address("cdn.example.net", "203.0.113.7", ttl=300.0),
            ],
        )
        self.assertEqual(
            upstream.wire,
            [("www.example.com", A, "udp"), ("cdn.example.net", A, "udp")],
        )

        second = resolver.resolve("www.example.com")

        self.assertTrue(second.cached)
        self.assertEqual(second.records, first.records)
        self.assertEqual(len(upstream.wire), 2)

    def test_a_cname_loop_is_reported_and_does_not_run_away(self):
        resolver, upstream, clock = build(
            {
                ("a.example.com", A): reply(
                    alias("a.example.com", "b.example.com", ttl=300.0)
                ),
                ("b.example.com", A): reply(
                    alias("b.example.com", "a.example.com", ttl=300.0)
                ),
            }
        )

        raised = None
        try:
            resolver.resolve("a.example.com")
        except ResolutionError as error:
            raised = error

        self.assertIsInstance(raised, CnameLoop)
        self.assertLessEqual(len(upstream.wire), 2)

    def test_a_truncated_answer_is_fetched_again_over_tcp(self):
        table = {
            ("big.example.com", A): reply(
                address("big.example.com", "203.0.113.1"),
                address("big.example.com", "203.0.113.2"),
                address("big.example.com", "203.0.113.3"),
            )
        }
        resolver, upstream, clock = build(table, truncated=[("big.example.com", A)])

        result = resolver.resolve("big.example.com")

        self.assertEqual(
            result.records,
            [
                address("big.example.com", "203.0.113.1"),
                address("big.example.com", "203.0.113.2"),
                address("big.example.com", "203.0.113.3"),
            ],
        )
        self.assertEqual(
            upstream.wire,
            [("big.example.com", A, "udp"), ("big.example.com", A, "tcp")],
        )

        again = resolver.resolve("big.example.com")

        self.assertTrue(again.cached)
        self.assertEqual(len(again.records), 3)
        self.assertEqual(len(upstream.wire), 2)

    def test_a_cached_chain_keeps_the_shortest_record_ttl(self):
        table = {
            ("www.example.com", A): reply(
                alias("www.example.com", "big.example.net", ttl=100.0)
            ),
            ("big.example.net", A): reply(
                address("big.example.net", "203.0.113.1"),
                address("big.example.net", "203.0.113.2"),
                address("big.example.net", "203.0.113.3"),
            ),
        }
        resolver, upstream, clock = build(table, truncated=[("big.example.net", A)])

        first = resolver.resolve("www.example.com")

        self.assertEqual(
            [record.rtype for record in first.records], [CNAME, A, A, A]
        )
        self.assertEqual(
            upstream.wire,
            [
                ("www.example.com", A, "udp"),
                ("big.example.net", A, "udp"),
                ("big.example.net", A, "tcp"),
            ],
        )

        clock.advance(50.0)
        alive = resolver.resolve("www.example.com")

        self.assertTrue(alive.cached)
        self.assertEqual(
            [record.ttl for record in alive.records], [50.0, 250.0, 250.0, 250.0]
        )

        clock.advance(100.0)
        refreshed = resolver.resolve("www.example.com")

        self.assertEqual(len(refreshed.records), 4)
        self.assertEqual(len(upstream.wire), 4)


class BatchTests(unittest.TestCase):
    def test_a_repeated_question_in_one_batch_costs_one_exchange(self):
        resolver, upstream, clock = build(
            {("www.example.com", A): reply(address("www.example.com"))}
        )

        first, second = resolver.resolve_many(
            [("WWW.Example.COM", A), ("www.example.com", A)]
        )

        self.assertEqual(first.records, second.records)
        self.assertEqual(upstream.wire, [("www.example.com", A, "udp")])

    def test_questions_of_different_types_asked_together_stay_apart(self):
        table = {
            ("www.example.com", A): reply(address("www.example.com")),
            ("www.example.com", AAAA): reply(
                Record("www.example.com", AAAA, 300.0, "2606:2800:220:1:248:1893:25c8:1946")
            ),
        }
        resolver, upstream, clock = build(table)

        v4, v6 = resolver.resolve_many(
            [("www.example.com", A), ("www.example.com", AAAA)]
        )

        self.assertEqual([record.rtype for record in v4.records], [A])
        self.assertEqual([record.rtype for record in v6.records], [AAAA])
        self.assertEqual(
            upstream.wire,
            [("www.example.com", A, "udp"), ("www.example.com", AAAA, "udp")],
        )


if __name__ == "__main__":
    unittest.main()
