"""Tests for matrix/buttons.py: button replacement."""

from __future__ import annotations

from ductor_bot.matrix.buttons import ButtonTracker


class TestButtonTracker:
    def test_extract_single_button(self) -> None:
        bt = ButtonTracker()
        result = bt.extract_and_format("!room:s", "Pick one [button:Yes] [button:No]")
        assert "1. Yes" in result
        assert "2. No" in result
        assert "[button:" not in result

    def test_no_buttons_returns_unchanged(self) -> None:
        bt = ButtonTracker()
        text = "Just regular text"
        assert bt.extract_and_format("!room:s", text) == text

    def test_match_input_returns_label(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:Alpha] [button:Beta]")
        assert bt.match_input("!room:s", "1") == "Alpha"

    def test_match_input_second_option(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A] [button:B] [button:C]")
        assert bt.match_input("!room:s", "3") == "C"

    def test_match_consumes_buttons(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A] [button:B]")
        bt.match_input("!room:s", "1")
        # After consumption, no match
        assert bt.match_input("!room:s", "2") is None

    def test_match_invalid_number(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A]")
        assert bt.match_input("!room:s", "5") is None

    def test_match_non_numeric(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A]")
        assert bt.match_input("!room:s", "hello") is None

    def test_match_no_active_buttons(self) -> None:
        bt = ButtonTracker()
        assert bt.match_input("!room:s", "1") is None

    def test_clear_removes_buttons(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A]")
        bt.clear("!room:s")
        assert bt.match_input("!room:s", "1") is None

    def test_different_rooms_isolated(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!r1:s", "Q [button:X]")
        bt.extract_and_format("!r2:s", "Q [button:Y]")
        assert bt.match_input("!r1:s", "1") == "X"
        assert bt.match_input("!r2:s", "1") == "Y"
