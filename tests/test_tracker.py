"""Tests del núcleo Python: ISO 6346, normalizador y mapeo de fallos.

Se ejecutan sin navegador: `python -m unittest discover -s tests -v`
"""

from __future__ import annotations

import unittest

from tracker.containers import (
    check_digit_valid,
    compute_check_digit,
    parse,
    resolve_carrier,
    split_csv,
)
from tracker.extract import extract, flatten_scalars, parse_timestamp
from tracker.models import Carrier, ResultStatus, ResultsPayload, RunStats


class ContainerTests(unittest.TestCase):
    def test_check_digit_of_known_container(self):
        # CSQU3054383 es el ejemplo canónico del estándar ISO 6346.
        self.assertEqual(compute_check_digit("CSQU3054383"), 3)
        self.assertTrue(check_digit_valid("CSQU3054383"))

    def test_invalid_check_digit_is_detected(self):
        self.assertFalse(check_digit_valid("MSCU1234567"))
        self.assertFalse(check_digit_valid("MSCU12345"))

    def test_normalisation_and_prefix_resolution(self):
        parsed = parse("  mscu-123 456 7 ")
        self.assertEqual(parsed.number, "MSCU1234567")
        self.assertEqual(parsed.carrier, Carrier.MSC)
        self.assertTrue(parsed.well_formed)

    def test_all_three_carriers_resolve(self):
        self.assertEqual(resolve_carrier("MSCU1234567"), Carrier.MSC)
        self.assertEqual(resolve_carrier("MAEU1234567"), Carrier.MAERSK)
        self.assertEqual(resolve_carrier("MRKU1234567"), Carrier.MAERSK)
        self.assertEqual(resolve_carrier("HLCU1234567"), Carrier.HAPAG_LLOYD)
        self.assertEqual(resolve_carrier("SUDU1234567"), Carrier.HAPAG_LLOYD)
        self.assertEqual(resolve_carrier("ZZZZ1234567"), Carrier.UNKNOWN)

    def test_split_csv_tolerates_separators_and_spaces(self):
        raw = " MSCU1234567, MAEU 765 432 1 ;HLCU1111111\nMSCU1234567 "
        self.assertEqual(
            split_csv(raw),
            ["MSCU1234567", "MAEU7654321", "HLCU1111111"],
        )

    def test_split_csv_on_empty_input(self):
        self.assertEqual(split_csv(""), [])
        self.assertEqual(split_csv(None), [])


class ExtractionTests(unittest.TestCase):
    def test_nested_payload_is_normalised(self):
        payload = {
            "data": {
                "shipment": {"containerNumber": "MSCU1234567"},
                "events": [
                    {"eventDate": "2026-11-01T10:00:00Z", "eventName": "Gate Out",
                     "locationName": "Callao", "vesselName": "MSC PERU"},
                    {"eventDate": "2026-11-12T03:00:00Z", "eventName": "Discharged",
                     "locationName": "Shanghai", "vesselName": "MSC PERU"},
                ],
                "eta": "2026-11-30T00:00:00Z",
            }
        }
        outcome = extract(payload, "MSCU1234567")
        self.assertIsNotNone(outcome.data)
        self.assertEqual(outcome.event_count, 2)
        # El evento más reciente manda.
        self.assertEqual(outcome.data.current_status, "Discharged")
        self.assertEqual(outcome.data.location, "Shanghai")
        self.assertEqual(outcome.data.vessel, "MSC PERU")
        self.assertEqual(outcome.data.eta, "2026-11-30T00:00:00Z")
        self.assertEqual(outcome.data.last_update, "2026-11-12T03:00:00Z")

    def test_deeply_nested_object_keeps_parent_key(self):
        payload = {"events": [{
            "eventTime": "2026-10-02T12:00:00.000+0000",
            "eventType": "GATE_IN",
            "location": {"city": "Lima", "country": "PE"},
            "vessel": {"name": "MAERSK LIMA"},
        }]}
        outcome = extract(payload, "MRKU1234567")
        self.assertEqual(outcome.data.vessel, "MAERSK LIMA")
        self.assertEqual(outcome.data.location, "Lima")
        self.assertEqual(outcome.data.last_update, "2026-10-02T12:00:00Z")

    def test_epoch_seconds_are_parsed(self):
        payload = {"shipmentEvents": [
            {"description": "Empty to shipper", "eventDateTime": 1762000000, "locationName": "Callao"},
            {"description": "Vessel departure", "eventDateTime": 1762300000, "locationName": "Callao"},
        ]}
        outcome = extract(payload, "HLCU1111111")
        self.assertEqual(outcome.event_count, 2)
        self.assertEqual(outcome.data.current_status, "Vessel departure")

    def test_not_found_payload_is_classified(self):
        outcome = extract({"error": "Container not found"}, "MSCU1234567")
        self.assertTrue(outcome.not_found)
        self.assertIsNone(outcome.data)

    def test_block_payload_is_classified(self):
        outcome = extract({"message": "Access Denied Reference #18.abc"}, "MSCU1234567")
        self.assertTrue(outcome.blocked)
        self.assertIsNone(outcome.data)

    def test_irrelevant_json_is_not_an_event_collection(self):
        outcome = extract({"countries": [{"code": "PE", "name": "Peru"}]}, "MSCU1234567")
        self.assertIsNone(outcome.data)
        self.assertFalse(outcome.not_found)
        self.assertFalse(outcome.blocked)

    def test_flatten_prefers_parent_qualified_keys(self):
        flat = flatten_scalars({"vessel": {"name": "X", "imo": "123"}})
        self.assertEqual(flat["vesselname"], "X")
        self.assertEqual(flat["vesselimo"], "123")
        self.assertEqual(flat["name"], "X")

    def test_timestamp_parsing_variants(self):
        self.assertEqual(parse_timestamp(1762000000).year, 2025)
        self.assertEqual(parse_timestamp(1762000000000).year, 2025)
        self.assertEqual(parse_timestamp("2026-11-12T03:00:00Z").day, 12)
        self.assertEqual(parse_timestamp("12/11/2026").day, 12)
        self.assertIsNone(parse_timestamp("no es una fecha"))
        self.assertIsNone(parse_timestamp(None))


class StatusTests(unittest.TestCase):
    def test_resolved_and_failure_sets(self):
        self.assertTrue(ResultStatus.OK.is_resolved)
        self.assertTrue(ResultStatus.NOT_FOUND.is_resolved)
        self.assertTrue(ResultStatus.INVALID.is_resolved)
        self.assertFalse(ResultStatus.BLOCKED.is_resolved)
        self.assertTrue(ResultStatus.BLOCKED.is_failure)
        self.assertTrue(ResultStatus.TIMEOUT.is_failure)
        self.assertTrue(ResultStatus.ERROR.is_failure)
        self.assertFalse(ResultStatus.OK.is_failure)

    def test_stats_count_each_bucket(self):
        payload = ResultsPayload(
            run_id="r", trigger="manual", started_at="a", finished_at="b",
            duration_ms=1, stats=RunStats(), results=[
                _result("MSCU1234567", ResultStatus.OK),
                _result("MAEU1234567", ResultStatus.NOT_FOUND),
                _result("HLCU1234567", ResultStatus.BLOCKED),
                _result("MSCU7654321", ResultStatus.TIMEOUT),
            ],
        )
        stats = payload.compute_stats()
        self.assertEqual((stats.ok, stats.not_found, stats.failed, stats.requested), (1, 1, 2, 4))


def _result(number: str, status: ResultStatus):
    from tracker.models import ContainerResult

    return ContainerResult(container_number=number, carrier="MSC", status=status.value)


if __name__ == "__main__":
    unittest.main()
