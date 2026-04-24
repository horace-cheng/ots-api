"""
Pure function tests for pricing and deadline logic in routers/orders.py.
No DB or HTTP involved.
"""

from routers.orders import _calc_price, _calc_deadline


class TestCalcPrice:
    def test_fast_track_basic(self):
        assert _calc_price("fast", 1000, "zh-tw") == 2000

    def test_fast_track_minimum_applied(self):
        # 10 words * NT$2 = NT$20, but minimum is NT$2,000
        assert _calc_price("fast", 10, "zh-tw") == 2000

    def test_fast_track_above_minimum(self):
        # 2000 words * NT$2 = NT$4,000
        assert _calc_price("fast", 2000, "zh-tw") == 4000

    def test_literary_track_basic(self):
        # 5000 words * NT$6 = NT$30,000
        assert _calc_price("literary", 5000, "zh-tw") == 30000

    def test_literary_track_minimum_applied(self):
        # 100 words * NT$6 = NT$600, minimum is NT$20,000
        assert _calc_price("literary", 100, "zh-tw") == 20000

    def test_japanese_multiplier_fast(self):
        # 1000 words * NT$2 * 1.2 = NT$2,400
        assert _calc_price("fast", 1000, "ja") == 2400

    def test_japanese_multiplier_literary(self):
        # 5000 words * NT$6 * 1.2 = NT$36,000
        assert _calc_price("literary", 5000, "ja") == 36000

    def test_japanese_minimum_still_applies(self):
        # 10 words * NT$6 * 1.2 = NT$72, minimum literary is NT$20,000
        assert _calc_price("literary", 10, "ja") == 20000

    def test_non_japanese_no_multiplier(self):
        assert _calc_price("fast", 1000, "en") == _calc_price("fast", 1000, "zh-tw")


class TestCalcDeadline:
    def test_fast_track_is_48h(self):
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc)
        deadline = _calc_deadline("fast")
        hours = (deadline - before).total_seconds() / 3600
        assert 47.9 <= hours <= 48.1

    def test_literary_track_is_30d(self):
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc)
        deadline = _calc_deadline("literary")
        days = (deadline - before).days
        assert days == 29 or days == 30  # allow for seconds boundary
