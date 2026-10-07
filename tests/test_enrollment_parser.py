"""Tests for enrollment parser, including unit inference and source parsing."""

from __future__ import annotations

from hightempbot.enrollment.parser import _parse_unit, parse_resolution_source


class TestParseUnit:
    def test_infers_c_from_market_question(self):
        event = {
            "description": "Daily highest temperature.",
            "markets": [
                {"question": "Will the highest temperature in Helsinki be 12°C or below on April 21?"},
                {"question": "Will the highest temperature in Helsinki be between 13-14°C on April 21?"},
            ],
        }
        assert _parse_unit(event["description"], event) == "C"

    def test_infers_f_from_market_question(self):
        event = {
            "description": "Daily highest temperature.",
            "markets": [
                {"question": "Will the highest temperature in Dallas be 72°F or below on April 21?"},
            ],
        }
        assert _parse_unit(event["description"], event) == "F"

    def test_description_fahrenheit_wins_when_no_markets(self):
        event = {"description": "Highest temperature in fahrenheit.", "markets": []}
        assert _parse_unit(event["description"], event) == "F"

    def test_description_degree_f_token_when_no_markets(self):
        event = {"description": "Highest °F reading.", "markets": []}
        assert _parse_unit(event["description"], event) == "F"

    def test_description_celsius_wins_when_no_markets(self):
        event = {"description": "Temperatura maxima em celsius.", "markets": []}
        assert _parse_unit(event["description"], event) == "C"

    def test_defaults_to_c_when_no_signals(self):
        event = {"description": "", "markets": []}
        assert _parse_unit("", event) == "C"

    def test_market_question_beats_description_silence(self):
        event = {
            "description": "",
            "markets": [{"question": "Will the highest temperature be 5°C on April 21?"}],
        }
        assert _parse_unit("", event) == "C"

    def test_first_market_with_unit_wins(self):
        event = {
            "markets": [
                {"question": "Will the highest temperature in Paris be 14°C on April 21?"},
                {"question": "Will the highest temperature in Dallas be 72°F on April 21?"},
            ],
        }
        assert _parse_unit("", event) == "C"

    def test_market_with_empty_question_is_skipped(self):
        event = {
            "markets": [
                {"question": ""},
                {"question": "Will the highest temperature in Helsinki be 12°C on April 21?"},
            ],
        }
        assert _parse_unit("", event) == "C"


class TestParseResolutionSource:
    def test_parses_wu_resolution_source(self):
        event = {
            "title": "Highest temperature in Dallas on April 21?",
            "description": "Daily highest temperature.",
            "resolutionSource": "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
            "markets": [
                {"question": "Will the highest temperature in Dallas be 72°F or below on April 21?"},
            ],
        }
        candidate = parse_resolution_source(event)
        assert candidate is not None
        assert candidate.icao == "KDAL"
        assert candidate.resolution_source == "wu"
        assert candidate.unit == "F"

    def test_parses_hko_from_description_when_resolution_source_is_null(self):
        event = {
            "title": "Highest temperature in Hong Kong on April 23?",
            "description": (
                "This market will resolve to the temperature range that contains the highest "
                "temperature recorded by the Hong Kong Observatory in degrees Celsius on 23 Apr '26.\n\n"
                'The resolution source for this market will be information from the Hong Kong Observatory, '
                'specifically the "Absolute Daily Max (deg. C)" once information is finalized in the '
                "relevant Daily Extract, available here: https://www.weather.gov.hk/en/cis/climat.htm"
            ),
            "resolutionSource": None,
            "markets": [
                {"question": "Will the highest temperature in Hong Kong be 24°C or below on April 23?"},
            ],
        }
        candidate = parse_resolution_source(event)
        assert candidate is not None
        assert candidate.city == "Hong Kong"
        assert candidate.icao == "VHHH"
        assert candidate.resolution_source == "hko"
        assert candidate.unit == "C"
