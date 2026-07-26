from hermes import Timeframe


def test_parse_units():
    assert Timeframe.parse("15m").seconds == 900
    assert Timeframe.parse("1h").seconds == 3600
    assert Timeframe.parse("1d").seconds == 86_400
    assert Timeframe.parse("1w").seconds == 604_800


def test_ordering():
    assert Timeframe.parse("15m") < Timeframe.parse("1h") < Timeframe.parse("1d")


def test_integer_multiple_constraint():
    base = Timeframe.parse("15m")
    assert Timeframe.parse("1h").is_multiple_of(base)   # 4x
    assert Timeframe.parse("30m").is_multiple_of(base)  # 2x
    assert not Timeframe.parse("45m").is_multiple_of(Timeframe.parse("2h"))


def test_roundtrip_str():
    assert str(Timeframe.parse("4h")) == "4h"
    assert str(Timeframe.parse("15m")) == "15m"
