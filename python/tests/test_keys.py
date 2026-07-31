from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

import pytest

from timeseries_cache import CacheKey, InvalidKwargError
from timeseries_cache.keys import canonicalize


class Vendor(Enum):
    BLOOMBERG = "bbg"
    REFINITIV = "refinitiv"


def digest(**kwargs) -> str:
    return CacheKey.build(kwargs).digest


class TestDeterminism:
    def test_keyword_order_is_irrelevant(self):
        assert digest(ticker="AAPL", field="close") == digest(
            field="close", ticker="AAPL"
        )

    def test_stable_across_processes(self):
        """The property that makes a cache directory reusable tomorrow.

        Run in a subprocess with a different PYTHONHASHSEED: anything built on
        `hash()` would diverge here.
        """
        script = (
            "from timeseries_cache import CacheKey;"
            "print(CacheKey.build({'ticker': 'AAPL', 'n': 3}).digest)"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            ).stdout.strip()
            for seed in ("0", "1", "12345")
        }
        assert len(runs) == 1
        assert runs.pop() == digest(ticker="AAPL", n=3)

    def test_different_kwargs_give_different_keys(self):
        assert digest(ticker="AAPL") != digest(ticker="MSFT")

    def test_empty_kwargs_are_allowed(self):
        assert digest()


class TestTypeTagging:
    def test_int_and_string_do_not_collide(self):
        assert digest(n=1) != digest(n="1")

    def test_bool_and_int_do_not_collide(self):
        # bool is an int subclass; without an explicit tag True renders as 1.
        assert digest(flag=True) != digest(flag=1)

    def test_none_and_the_string_none_do_not_collide(self):
        assert digest(x=None) != digest(x="None")

    def test_float_and_int_do_not_collide(self):
        assert digest(n=1) != digest(n=1.0)

    def test_missing_kwarg_differs_from_none(self):
        assert digest(a=1) != digest(a=1, b=None)

    def test_list_order_matters(self):
        assert digest(cols=["a", "b"]) != digest(cols=["b", "a"])

    def test_list_and_tuple_agree(self):
        assert digest(cols=["a", "b"]) == digest(cols=("a", "b"))

    def test_nested_separator_is_not_forgeable(self):
        # A naive "join with &" scheme would let a value impersonate a kwarg.
        assert digest(a="1&b=2") != digest(a="1", b="2")


class TestSupportedTypes:
    @pytest.mark.parametrize(
        "value",
        [
            "AAPL",
            7,
            -7,
            3.25,
            True,
            False,
            None,
            date(2024, 1, 1),
            datetime(2024, 1, 1, tzinfo=UTC),
            Decimal("1.50"),
            Vendor.BLOOMBERG,
            ["a", 1, None],
            ("a", 1, None),
        ],
    )
    def test_canonicalizes(self, value):
        assert digest(v=value)

    def test_enum_uses_its_value(self):
        assert digest(vendor=Vendor.BLOOMBERG) == digest(vendor="bbg")

    def test_timezone_aware_datetimes_normalize_to_utc(self):
        from datetime import timedelta, timezone

        eastern = timezone(timedelta(hours=-5))
        assert digest(t=datetime(2024, 1, 1, 19, tzinfo=eastern)) == digest(
            t=datetime(2024, 1, 2, 0, tzinfo=UTC)
        )


class TestRejections:
    def test_rejects_reserved_names(self):
        for name in ("start", "end", "mode", "columns", "frame"):
            with pytest.raises(InvalidKwargError, match="reserved"):
                canonicalize({name: "x"})

    def test_rejects_sets(self):
        with pytest.raises(InvalidKwargError, match="reliable ordering"):
            canonicalize({"tags": {"a", "b"}})

    def test_rejects_nested_dicts(self):
        with pytest.raises(InvalidKwargError, match="nested mappings"):
            canonicalize({"opts": {"a": 1}})

    def test_rejects_nan(self):
        with pytest.raises(InvalidKwargError, match="stable identity"):
            canonicalize({"x": float("nan")})

    def test_rejects_infinity(self):
        with pytest.raises(InvalidKwargError, match="stable identity"):
            canonicalize({"x": float("inf")})

    def test_rejects_naive_datetime(self):
        from timeseries_cache import WindowError

        with pytest.raises(WindowError, match="timezone-aware"):
            canonicalize({"t": datetime(2024, 1, 1)})

    def test_rejects_arbitrary_objects(self):
        with pytest.raises(InvalidKwargError, match="deterministic canonical form"):
            canonicalize({"x": object()})

    def test_rejects_bad_value_inside_a_list(self):
        with pytest.raises(InvalidKwargError, match=r"cols\[1\]"):
            canonicalize({"cols": ["a", object()]})


class TestPathLayout:
    def test_shards_by_digest_prefix(self):
        key = CacheKey.build({"ticker": "AAPL"})
        assert key.relative_path == f"{key.digest[:2]}/{key.digest}"
        assert key.shard == key.digest[:2]

    def test_keeps_kwargs_verbatim_for_collision_detection(self):
        key = CacheKey.build({"ticker": "AAPL", "n": 3})
        assert key.kwargs == {"ticker": "AAPL", "n": 3}
