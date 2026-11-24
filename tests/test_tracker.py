"""Tests del núcleo Python: ISO 6346, normalizador y mapeo de fallos.

Se ejecutan sin navegador: `python -m unittest discover -s tests -v`
"""

from __future__ import annotations

import unittest

from tracker.containers import (
    carrier_candidates,
    check_digit_valid,
    compute_check_digit,
    parse,
    resolve_carrier,
    split_csv,
)
from tracker.extract import extract, flatten_scalars, parse_timestamp
from tracker.httpclient import parse_pending_entries
from tracker.models import Carrier, ResultStatus, ResultsPayload, RunStats

from scraper import (
    build_plan,
    carrier_attempts,
    group_by_proxy,
    order_results,
    should_try_alternative,
)

from tracker.config import load_settings

from tracker.hapag import (
    HAPAG_STATES,
    container_param,
    listing_url,
    parse_movements,
    rows_from_html,
    to_payload,
)


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


class LeasingPrefixTests(unittest.TestCase):
    """Los prefijos de arrendador (CAIU, DRYU, FCIU...) no identifican naviera.

    Medido con el B/L MEDUADP85690 de MSC: 6 de sus 8 contenedores usan
    prefijos de leasing y se descartaban como INVALID antes de consultar nada.
    La naviera se conoce por el B/L o por el backend, no por el prefijo.
    """

    def test_leasing_prefix_without_hint_is_unknown(self):
        # El prefijo por si solo no basta: CAIU puede ir en un barco de MSC o
        # de cualquier otra. Sin dato externo, no se inventa una naviera.
        self.assertEqual(parse("CAIU3430650").carrier, Carrier.UNKNOWN)

    def test_explicit_carrier_resolves_leasing_prefix(self):
        parsed = parse("CAIU3430650", carrier_hint="MSC")
        self.assertEqual(parsed.carrier, Carrier.MSC)
        self.assertTrue(parsed.well_formed)
        self.assertTrue(parsed.check_digit_ok)

    def test_hint_does_not_override_a_carrier_specific_prefix(self):
        # MRKU es de Maersk según BIC. Una pista equivocada no debe romper un
        # caso que ya funcionaba: el prefijo propio es dato autoritativo.
        self.assertEqual(parse("MRKU7779494", carrier_hint="MSC").carrier, Carrier.MAERSK)

    def test_hint_accepts_the_carrier_enum_too(self):
        self.assertEqual(parse("FTAU1546275", carrier_hint=Carrier.HAPAG_LLOYD).carrier,
                         Carrier.HAPAG_LLOYD)

    def test_unknown_hint_value_is_ignored(self):
        self.assertEqual(parse("CAIU3430650", carrier_hint="NO_EXISTE").carrier, Carrier.UNKNOWN)

    def test_candidates_for_known_prefix_is_just_that_carrier(self):
        allowed = (Carrier.MSC, Carrier.MAERSK)
        self.assertEqual(carrier_candidates("MRKU7779494", allowed), [Carrier.MAERSK])

    def test_candidates_for_ambiguous_prefix_covers_the_whitelist(self):
        # Sin dato externo se prueban las navieras habilitadas, en su orden.
        allowed = (Carrier.MSC, Carrier.MAERSK)
        self.assertEqual(carrier_candidates("CAIU3430650", allowed),
                         [Carrier.MSC, Carrier.MAERSK])

    def test_candidates_with_hint_are_pinned(self):
        allowed = (Carrier.MSC, Carrier.MAERSK)
        self.assertEqual(carrier_candidates("CAIU3430650", allowed, carrier_hint="MSC"),
                         [Carrier.MSC])


class PendingEntriesTests(unittest.TestCase):
    """El backend ya sabe la naviera; no hay que re-adivinarla por el prefijo.

    `InMemoryContainerTrackingRepository.carrierOf` usa la misma tabla de
    prefijos que el scraper, así que para un CAIU devuelve UNKNOWN. Aun así,
    cuando el backend sí manda una naviera usable hay que respetarla: es el
    único sitio donde puede llegar el dato del B/L.
    """

    def test_reads_number_and_carrier_together(self):
        entries = [
            {"containerNumber": "CAIU3430650", "carrier": "MSC", "attempts": 0},
            {"containerNumber": "HLBU2959982", "carrier": "HAPAG_LLOYD", "attempts": 1},
        ]
        self.assertEqual(
            parse_pending_entries(entries),
            [("CAIU3430650", "MSC"), ("HLBU2959982", "HAPAG_LLOYD")],
        )

    def test_plain_strings_still_supported(self):
        self.assertEqual(parse_pending_entries(["MSCU1234567"]), [("MSCU1234567", None)])

    def test_unknown_carrier_is_dropped_so_prefix_can_decide(self):
        entries = [{"containerNumber": "CAIU3430650", "carrier": "UNKNOWN"}]
        self.assertEqual(parse_pending_entries(entries), [("CAIU3430650", None)])

    def test_missing_carrier_key_is_fine(self):
        self.assertEqual(parse_pending_entries([{"containerNumber": "X"}]), [("X", None)])

    def test_duplicates_are_removed_keeping_first(self):
        entries = [
            {"containerNumber": "CAIU3430650", "carrier": "MSC"},
            {"containerNumber": "caiu3430650", "carrier": "MAERSK"},
        ]
        self.assertEqual(parse_pending_entries(entries), [("CAIU3430650", "MSC")])


class BuildPlanTests(unittest.TestCase):
    """build_plan es donde se decidía descartar los prefijos de arrendador."""

    def test_leasing_prefix_with_backend_carrier_becomes_a_job(self):
        jobs, pre = build_plan(
            [("CAIU3430650", "MSC")],
            (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD),
            40,
        )
        self.assertEqual(pre, [])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].parsed.carrier, Carrier.MSC)

    def test_leasing_prefix_without_hint_tries_the_enabled_carriers(self):
        # Sin dato externo no se puede saber la naviera, así que se prueban las
        # habilitadas en orden en vez de descartar el contenedor sin consultar.
        jobs, pre = build_plan(
            [("CAIU3430650", None)],
            (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD),
            40,
        )
        self.assertEqual(pre, [])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].parsed.carrier, Carrier.MSC)
        self.assertEqual(jobs[0].alternatives, [Carrier.MAERSK, Carrier.HAPAG_LLOYD])

    def test_known_prefix_has_no_alternatives(self):
        jobs, _ = build_plan([("MRKU7779494", None)], (Carrier.MSC, Carrier.MAERSK), 40)
        self.assertEqual(jobs[0].alternatives, [])

    def test_hint_collapses_alternatives(self):
        # Con pista del backend no se prueba nada más: una sola consulta.
        jobs, _ = build_plan([("CAIU3430650", "MSC")], (Carrier.MSC, Carrier.MAERSK), 40)
        self.assertEqual(jobs[0].alternatives, [])

    def test_plain_strings_keep_working_unchanged(self):
        jobs, pre = build_plan(["MRKU7779494"], (Carrier.MAERSK,), 40)
        self.assertEqual(pre, [])
        self.assertEqual(jobs[0].parsed.carrier, Carrier.MAERSK)

    def test_backend_carrier_does_not_override_a_specific_prefix(self):
        jobs, _ = build_plan([("MRKU7779494", "MSC")], (Carrier.MAERSK,), 40)
        self.assertEqual(jobs[0].parsed.carrier, Carrier.MAERSK)

    def test_carrier_filter_limits_the_alternatives(self):
        # Si el usuario pidió solo MSC, no se prueba Maersk ni Hapag.
        jobs, _ = build_plan([("CAIU3430650", None)], (Carrier.MSC,), 40)
        self.assertEqual(jobs[0].parsed.carrier, Carrier.MSC)
        self.assertEqual(jobs[0].alternatives, [])

    def test_no_enabled_carrier_at_all_is_reported_unknown(self):
        jobs, pre = build_plan([("CAIU3430650", None)], (), 40)
        self.assertEqual(jobs, [])
        self.assertEqual(pre[0].error_code, "UNKNOWN_CARRIER")


class AlternativeCarrierTests(unittest.TestCase):
    """Cuándo pasar a la siguiente naviera de un prefijo de arrendador.

    Probar otra naviera cuesta una consulta real, así que solo se hace cuando
    la anterior dijo claramente "este contenedor no es mío". Un bloqueo no es
    eso: reintentar con otra naviera desde la misma IP solo empeora el bloqueo.
    """

    def test_not_found_moves_to_the_next_carrier(self):
        self.assertTrue(should_try_alternative("NOT_FOUND", "NOT_FOUND"))

    def test_ok_does_not_move(self):
        self.assertFalse(should_try_alternative("OK", None))

    def test_blocked_does_not_move(self):
        self.assertFalse(should_try_alternative("BLOCKED", "ANTIBOT_CHALLENGE"))

    def test_invalid_does_not_move(self):
        self.assertFalse(should_try_alternative("INVALID", "INVALID_FORMAT"))

    def test_timeout_does_not_move(self):
        # Sin datos del formulario no se sabe si la naviera era la correcta;
        # seguir probando solo gasta presupuesto.
        self.assertFalse(should_try_alternative("TIMEOUT", "NO_TRACKING_DATA"))


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


class ArrivalTests(unittest.TestCase):
    """La pregunta del negocio es "¿el buque ya llegó al puerto?".

    El contrato anterior solo exponía currentStatus = último evento. Para
    MRKU7779494 eso daba "CX" en MEXICO CITY, que es el trayecto terrestre
    posterior, no la descarga en Manzanillo. Inservible para la pregunta real.
    """

    def _maersk_like(self):
        return {
            "containers": [{
                "container_num": "MRKU7779494",
                "events": [
                    {"date": "2026-09-03T15:32:00Z", "status": "37",
                     "location": "QINGDAO", "vessel": "MAERSK EL ALTO", "voyage": "636E"},
                    {"date": "2026-10-06T12:28:00Z", "status": "Discharged",
                     "location": "MANZANILLO", "vessel": "MAERSK EL ALTO", "voyage": "636E"},
                    {"date": "2026-10-27T07:19:00Z", "status": "CX",
                     "location": "MEXICO CITY"},
                ],
            }]
        }

    def test_discharge_at_port_marks_arrival(self):
        outcome = extract(self._maersk_like(), "MRKU7779494", port="MANZANILLO")
        self.assertIsNotNone(outcome.data)
        self.assertTrue(outcome.data.arrived)
        self.assertEqual(outcome.data.arrival_date, "2026-10-06T12:28:00Z")
        self.assertEqual(outcome.data.discharge_port, "MANZANILLO")

    def test_latest_event_is_still_available(self):
        # El último evento sigue siendo útil, solo que no responde "llegó".
        data = extract(self._maersk_like(), "MRKU7779494", port="MANZANILLO").data
        self.assertEqual(data.current_status, "CX")
        self.assertEqual(data.location, "MEXICO CITY")

    def test_vessel_and_voyage_come_from_the_discharge_event(self):
        # El buque que descargó, no el del último evento terrestre.
        data = extract(self._maersk_like(), "MRKU7779494", port="MANZANILLO").data
        self.assertEqual(data.vessel, "MAERSK EL ALTO")
        self.assertEqual(data.voyage, "636E")

    def test_without_discharge_at_port_it_has_not_arrived(self):
        payload = {"containers": [{
            "container_num": "MRKU7779494",
            "events": [
                {"date": "2026-09-03T15:32:00Z", "status": "Loaded", "location": "QINGDAO"},
                {"date": "2026-10-01T00:00:00Z", "status": "Vessel departed",
                 "location": "BUSAN"},
            ],
        }]}
        data = extract(payload, "MRKU7779494", port="MANZANILLO").data
        self.assertFalse(data.arrived)
        self.assertIsNone(data.arrival_date)

    def test_discharge_in_another_port_is_not_an_arrival(self):
        # Llegó a otro puerto: no responde la pregunta de Manzanillo.
        payload = {"containers": [{
            "container_num": "MRKU7779494",
            "events": [
                {"date": "2026-10-06T12:28:00Z", "status": "Discharged",
                 "location": "CALLAO"},
            ],
        }]}
        data = extract(payload, "MRKU7779494", port="MANZANILLO").data
        self.assertFalse(data.arrived)
        self.assertEqual(data.discharge_port, "MANZANILLO")

    def test_earliest_arrival_wins_when_repeated(self):
        # Un contenedor puede descargarse, salir y volver. La primera llegada
        # al puerto es la respuesta a "¿cuándo llegó?".
        payload = {"containers": [{
            "container_num": "MRKU7779494",
            "events": [
                {"date": "2026-10-10T00:00:00Z", "status": "Discharged",
                 "location": "MANZANILLO"},
                {"date": "2026-10-06T12:28:00Z", "status": "Discharged",
                 "location": "MANZANILLO"},
            ],
        }]}
        data = extract(payload, "MRKU7779494", port="MANZANILLO").data
        self.assertEqual(data.arrival_date, "2026-10-06T12:28:00Z")

    def test_no_port_configured_does_not_claim_arrival(self):
        # Sin puerto no hay comparación posible: mejor no afirmar nada.
        data = extract(self._maersk_like(), "MRKU7779494", port=None).data
        self.assertFalse(data.arrived)

    def test_serialised_payload_exposes_arrival(self):
        data = extract(self._maersk_like(), "MRKU7779494", port="MANZANILLO").data
        wire = data.to_dict()
        self.assertTrue(wire["arrived"])
        self.assertEqual(wire["arrivalDate"], "2026-10-06T12:28:00Z")
        self.assertEqual(wire["dischargePort"], "MANZANILLO")


class PortConfigTests(unittest.TestCase):
    """El puerto de descarga que interesa es configurable.

    Por defecto Manzanillo, que es el caso real, pero el mismo scraper sirve
    para cualquier puerto sin tocar codigo.
    """

    def test_default_port_is_manzanillo(self):
        settings = load_settings(["--containers", "MSCU1234567"])
        self.assertEqual(settings.discharge_port, "MANZANILLO")

    def test_port_comes_from_env(self):
        import os
        previous = os.environ.get("SCRAPER_PORT")
        os.environ["SCRAPER_PORT"] = "callao"
        try:
            settings = load_settings(["--containers", "MSCU1234567"])
            self.assertEqual(settings.discharge_port, "CALLAO")
        finally:
            if previous is None:
                os.environ.pop("SCRAPER_PORT", None)
            else:
                os.environ["SCRAPER_PORT"] = previous

    def test_empty_port_disables_the_check(self):
        import os
        previous = os.environ.get("SCRAPER_PORT")
        os.environ["SCRAPER_PORT"] = ""
        try:
            settings = load_settings(["--containers", "MSCU1234567"])
            self.assertIsNone(settings.discharge_port)
        finally:
            if previous is None:
                os.environ.pop("SCRAPER_PORT", None)
            else:
                os.environ["SCRAPER_PORT"] = previous


class MaerskNestedShapeTests(unittest.TestCase):
    """Forma real de Maersk: los eventos cuelgan de locations[].events[].

    Verificado con el payload de 275792562. El evento es
    {"activity": "DISCHARG", ...} y el registro de ubicación que lo contiene
    tiene {"city": "MANZANILLO", "state": "CL"}. `state` es el estado/provincia
    mexicano (Colima), NO un estado de envío.

    Sin esto, el extractor tomaba el registro de ubicación como si fuera el
    evento: status quedaba en "CL" y nunca se veía ninguna descarga.
    """

    def _payload(self):
        return {
            "containers": [{
                "container_num": "MRKU7779494",
                "locations": [
                    {
                        "city": "QINGDAO", "state": "37", "location_code": "CNTSTQQ",
                        "events": [{
                            "activity": "LOAD", "event_time": "2026-09-04T02:20:00.000",
                            "vessel_name": "MAERSK EL ALTO", "voyage_num": "636E",
                        }],
                    },
                    {
                        "city": "MANZANILLO", "state": "CL", "location_code": "MXZLOSM",
                        "events": [
                            {
                                "activity": "CONTAINER ARRIVAL",
                                "event_time": "2026-10-06T12:28:00.000",
                                "vessel_name": "MAERSK EL ALTO", "voyage_num": "636E",
                            },
                            {
                                "activity": "DISCHARG",
                                "event_time": "2026-10-06T23:14:00.000",
                                "vessel_name": "MAERSK EL ALTO", "voyage_num": "636E",
                            },
                        ],
                    },
                ],
            }]
        }

    def test_state_code_is_not_mistaken_for_status(self):
        data = extract(self._payload(), "MRKU7779494", port="MANZANILLO").data
        statuses = [e.status for e in data.events]
        self.assertNotIn("CL", statuses)
        self.assertNotIn("37", statuses)

    def test_discharge_is_detected_in_the_nested_shape(self):
        data = extract(self._payload(), "MRKU7779494", port="MANZANILLO").data
        self.assertTrue(data.arrived)
        # La llegada al puerto es el CONTAINER ARRIVAL (12:28), que ocurre
        # antes de la descarga (23:14). El contrato normaliza a ISO-8601 Z.
        self.assertEqual(parse_timestamp(data.arrival_date),
                         parse_timestamp("2026-10-06T12:28:00Z"))

    def test_event_inherits_the_city_of_its_parent_location(self):
        # El evento solo trae locationCode (MXZLOSM); la ciudad está en el
        # registro padre. Sin heredarla no se puede comparar con MANZANILLO.
        data = extract(self._payload(), "MRKU7779494", port="MANZANILLO").data
        arrivals = [e for e in data.events if e.status == "CONTAINER ARRIVAL"]
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0].location, "MANZANILLO")

    def test_location_code_is_kept_as_fallback(self):
        # Si no hubiera ciudad, el código sigue sirviendo para depurar.
        payload = {"containers": [{
            "container_num": "MRKU7779494",
            "locations": [{
                "location_code": "MXZLOSM",
                "events": [{"activity": "DISCHARG", "event_time": "2026-10-06T23:14:00Z"}],
            }],
        }]}
        data = extract(payload, "MRKU7779494", port="MXZLOSM").data
        self.assertTrue(data.arrived)

    def test_vessel_comes_from_the_discharge_event_not_a_number(self):
        data = extract(self._payload(), "MRKU7779494", port="MANZANILLO").data
        self.assertEqual(data.vessel, "MAERSK EL ALTO")
        self.assertEqual(data.voyage, "636E")


class VesselNameTests(unittest.TestCase):
    """El buque debe ser un nombre, no el IMO del objeto Vessel de MSC.

    Verificado con MEDUADP85690: el evento de descarga trae
    Detail=["MSC LAUSANNE VI", "QM639R"] y además
    Vessel={"IMO": "9320398", "Built": "2005", ...}. Quedarse con el IMO no
    sirve para nada operativo.
    """

    def test_imo_is_never_used_as_vessel_name(self):
        payload = {"containers": [{
            "container_num": "MEDU3037855",
            "events": [{
                "description": "Import Discharged from Vessel",
                "date": "2026-10-14T00:00:00Z",
                "location": "MANZANILLO, MX",
                "detail": ["MSC LAUSANNE VI", "QM639R"],
                "vessel": {"imo": "9320398", "built": "2005", "flag": "LR"},
            }],
        }]}
        data = extract(payload, "MEDU3037855", port="MANZANILLO").data
        self.assertTrue(data.arrived)
        self.assertEqual(data.vessel, "MSC LAUSANNE VI")
        self.assertEqual(data.voyage, "QM639R")

    def test_pure_numeric_vessel_is_rejected(self):
        payload = {"containers": [{
            "container_num": "MEDU3037855",
            "events": [{
                "description": "Discharged", "date": "2026-10-14T00:00:00Z",
                "location": "MANZANILLO", "vessel": "9320398",
            }],
        }]}
        data = extract(payload, "MEDU3037855", port="MANZANILLO").data
        self.assertIsNone(data.vessel)


class EtaSemanticsTests(unittest.TestCase):
    """`eta` debe ser la llegada al PUERTO, no la entrega final.

    Verificado con 275792562: el payload trae
    eta_final_delivery = 2026-10-31T13:57:00Z, que es la entrega al
    consignatario en MEXICO CITY, cuatro semanas DESPUÉS de la descarga en
    Manzanillo (06/10). Reportarlo como "ETA del puerto" es falso y además
    llega tarde: cuando se publica, el contenedor ya está en el puerto.
    """

    def test_final_delivery_is_not_reported_as_port_eta(self):
        payload = {"containers": [{
            "container_num": "MRKU7779494",
            "eta_final_delivery": "2026-10-31T13:57:00.000",
            "locations": [{
                "city": "MANZANILLO",
                "events": [{"activity": "DISCHARG",
                            "event_time": "2026-10-06T23:14:00.000"}],
            }],
        }]}
        data = extract(payload, "MRKU7779494", port="MANZANILLO").data
        self.assertNotEqual(data.eta, "2026-10-31T13:57:00Z")

    def test_final_delivery_is_kept_in_its_own_field(self):
        # No se descarta: es un dato útil, solo que no es la ETA del puerto.
        payload = {"containers": [{
            "container_num": "MRKU7779494",
            "eta_final_delivery": "2026-10-31T13:57:00.000",
            "locations": [{
                "city": "MANZANILLO",
                "events": [{"activity": "DISCHARG",
                            "event_time": "2026-10-06T23:14:00.000"}],
            }],
        }]}
        data = extract(payload, "MRKU7779494", port="MANZANILLO").data
        self.assertEqual(parse_timestamp(data.final_delivery_eta),
                         parse_timestamp("2026-10-31T13:57:00Z"))

    def test_real_port_eta_is_still_used_while_in_transit(self):
        # Aún no ha llegado: aquí la ETA del puerto SÍ es el dato útil.
        payload = {"containers": [{
            "container_num": "MSCU1234567",
            "pod_eta_date": "2026-10-05T00:00:00Z",
            "eta_final_delivery": "2026-10-31T13:57:00.000",
            "locations": [{
                "city": "QINGDAO",
                "events": [{"activity": "LOAD",
                            "event_time": "2026-09-04T02:20:00.000"}],
            }],
        }]}
        data = extract(payload, "MSCU1234567", port="MANZANILLO").data
        self.assertFalse(data.arrived)
        self.assertEqual(parse_timestamp(data.eta),
                         parse_timestamp("2026-10-05T00:00:00Z"))

    def test_eta_is_dropped_once_the_container_already_arrived(self):
        # Si ya llegó, una ETA deja de tener sentido: se conoce la fecha real.
        payload = {"containers": [{
            "container_num": "MSCU1234567",
            "pod_eta_date": "2026-10-05T00:00:00Z",
            "locations": [{
                "city": "MANZANILLO",
                "events": [{"activity": "DISCHARG",
                            "event_time": "2026-10-06T23:14:00.000"}],
            }],
        }]}
        data = extract(payload, "MSCU1234567", port="MANZANILLO").data
        self.assertTrue(data.arrived)
        self.assertIsNone(data.eta)


# HTML real del listado legacy: el contenedor viene con DOS espacios porque el
# servidor los emite para alinear la columna, y el submit los convierte en `++`.
HAPAG_LISTING_HTML = """
<table>
  <tr class="x-grid-row">
    <td><input type="radio" id="r1" name="row"/></td>
    <td><span class="nonEditableContent">HLBU  2959982</span></td>
    <td>40 High Cube</td>
    <td>Discharged</td>
  </tr>
  <tr class="x-grid-row">
    <td><input type="radio" id="r2" name="row"/></td>
    <td><span class="nonEditableContent">FTAU  1546275</span></td>
    <td>20 Dry</td>
    <td>Loaded</td>
  </tr>
  <tr class="x-grid-header"><td>Cabecera sin radio</td></tr>
</table>
"""

# Texto de la tabla de movimientos tal como queda en innerText.
HAPAG_DETAIL_TEXT = """Hapag-Lloyd Tracking
Container HLBU2959982

Discharged\tMANZANILLO, COL (MX)\t2026-10-22\t20:38\tIQUIQUE EXPRESS\t2636E
Vessel departed\tMANZANILLO, COL (MX)\t2026-10-23\t04:10\tIQUIQUE EXPRESS\t2636E
Gate out full\tMANZANILLO, COL (MX)\t2026-10-24\t09:00\t\t
Vessel arrived\tMANZANILLO, COL (MX)\t2026-10-20\t15:45\tIQUIQUE EXPRESS\t2636E
Loaded\tQINGDAO\t2026-09-12\t16:20\tIQUIQUE EXPRESS\t2636E
"""


class HapagListingTests(unittest.TestCase):
    """El listado legacy de Hapag no es JSON: es HTML con radios y celdas.

    Se lee del HTML servido y no del DOM porque el número viene relleno con dos
    espacios (`HLBU  2959982`) y el DOM los colapsa. Esos dos espacios son
    significativos: el submit los codifica como `++`.
    """

    def test_reads_each_row_with_its_container(self):
        rows = rows_from_html(HAPAG_LISTING_HTML)
        self.assertEqual([r["container"] for r in rows], ["HLBU2959982", "FTAU1546275"])

    def test_ignores_rows_without_a_radio(self):
        # La cabecera de la tabla no es seleccionable y no debe entrar.
        self.assertEqual(len(rows_from_html(HAPAG_LISTING_HTML)), 2)

    def test_keeps_the_two_spaces_of_the_served_html(self):
        rows = rows_from_html(HAPAG_LISTING_HTML)
        self.assertEqual(rows[0]["param"], "HLBU++2959982")

    def test_container_param_matches_what_the_form_submits(self):
        self.assertEqual(container_param("HLBU  2959982"), "HLBU++2959982")

    def test_single_space_is_a_single_plus(self):
        # No se fuerza `++`: se traduce lo que haya, para no inventar el valor.
        self.assertEqual(container_param("HLBU 2959982"), "HLBU+2959982")

    def test_regular_spacing_needs_no_plus(self):
        self.assertEqual(container_param("HLBU2959982"), "HLBU2959982")

    def test_listing_url_by_container_uses_the_legacy_page(self):
        # El scraper recibe contenedores, no B/L: esta es la ruta normal.
        url = listing_url("HLBU  2959982")
        self.assertIn("track-by-container-solution-legacy.html", url)
        self.assertIn("container=HLBU++2959982", url)

    def test_listing_url_by_bl_uses_the_verified_page(self):
        # Variante verificada en produccion. Se deja disponible porque es la
        # unica que se ha comprobado de punta a punta contra el sitio real.
        url = listing_url("HLBU2959982", bl="HLCUNG12607EKZE0")
        self.assertIn("track-by-booking-solution-legacy.html", url)
        self.assertIn("blno=HLCUNG12607EKZE0", url)

    def test_empty_html_gives_no_rows(self):
        self.assertEqual(rows_from_html(""), [])


class HapagMovementsTests(unittest.TestCase):
    """La tabla de movimientos: la llegada se decide por estado + puerto."""

    def test_parses_tab_separated_rows(self):
        events = parse_movements(HAPAG_DETAIL_TEXT)
        self.assertEqual(len(events), 5)
        first = events[0]
        self.assertEqual(first["activity"], "Discharged")
        self.assertEqual(first["city"], "MANZANILLO, COL (MX)")
        self.assertEqual(first["vessel_name"], "IQUIQUE EXPRESS")
        self.assertEqual(first["voyage_num"], "2636E")

    def test_ignores_lines_that_are_not_movements(self):
        # "Hapag-Lloyd Tracking" y "Container ..." no son estados.
        for event in parse_movements(HAPAG_DETAIL_TEXT):
            self.assertIn(event["activity"], HAPAG_STATES)

    def test_parses_space_separated_rows_without_tabs(self):
        # Algunas veces el texto llega sin tabuladores; la fecha es el ancla.
        text = "Discharged MANZANILLO, COL (MX) 2026-10-22 20:38 IQUIQUE EXPRESS 2636E"
        events = parse_movements(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["activity"], "Discharged")
        self.assertEqual(events[0]["city"], "MANZANILLO, COL (MX)")

    def test_joins_date_and_time(self):
        events = parse_movements(HAPAG_DETAIL_TEXT)
        self.assertEqual(events[0]["event_time"], "2026-10-22 20:38")

    def test_blank_text_gives_nothing(self):
        self.assertEqual(parse_movements(""), [])

    def test_payload_is_digestible_by_the_normal_extractor(self):
        # Lo importante: no se duplica la deteccion de llegada. Los movimientos
        # se convierten al mismo esquema que MSC y Maersk y decide `extract`.
        events = parse_movements(HAPAG_DETAIL_TEXT)
        payload = to_payload("HLBU2959982", events)
        data = extract(payload, "HLBU2959982", port="MANZANILLO").data
        self.assertIsNotNone(data)
        self.assertTrue(data.arrived)
        # "Vessel arrived" (20/10 15:45) es antes que "Discharged" (22/10).
        self.assertEqual(parse_timestamp(data.arrival_date),
                         parse_timestamp("2026-10-20T15:45:00"))

    def test_vessel_and_voyage_come_from_the_arrival_event(self):
        events = parse_movements(HAPAG_DETAIL_TEXT)
        data = extract(to_payload("HLBU2959982", events), "HLBU2959982",
                       port="MANZANILLO").data
        self.assertEqual(data.vessel, "IQUIQUE EXPRESS")
        self.assertEqual(data.voyage, "2636E")

    def test_movements_in_another_port_are_not_an_arrival(self):
        events = parse_movements(HAPAG_DETAIL_TEXT)
        data = extract(to_payload("HLBU2959982", events), "HLBU2959982",
                       port="CALLAO").data
        self.assertFalse(data.arrived)

    def test_no_movements_means_no_made_up_payload(self):
        self.assertIsNone(to_payload("HLBU2959982", []))

    def test_city_with_country_suffix_still_matches_the_port(self):
        # Hapag escribe "MANZANILLO, COL (MX)"; el puerto configurado es
        # "MANZANILLO". La comparacion no puede ser de igualdad exacta.
        events = parse_movements(HAPAG_DETAIL_TEXT)
        data = extract(to_payload("HLBU2959982", events), "HLBU2959982",
                       port="MANZANILLO").data
        self.assertTrue(data.arrived)
        self.assertEqual(data.discharge_port, "MANZANILLO")


class PerCarrierProxyTests(unittest.TestCase):
    """El proxy va por naviera, no global.

    Medido en las pruebas de acceso: Hapag-Lloyd bloquea por reputación de IP y
    necesita WARP, pero MSC y Maersk responden bien desde la IP del runner y
    meterlos por WARP los degrada (MSC falló de forma intermitente saliendo por
    WARP). Aplicar el proxy a los tres era la causa de ese fallo.
    """

    def _settings(self, **env):
        import os
        previous = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return load_settings(["--containers", "MSCU1234567"])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_default_proxies_only_hapag(self):
        settings = load_settings(["--containers", "MSCU1234567"])
        self.assertEqual(settings.proxy_for(Carrier.HAPAG_LLOYD),
                         "socks5://127.0.0.1:40000")
        self.assertIsNone(settings.proxy_for(Carrier.MSC))
        self.assertIsNone(settings.proxy_for(Carrier.MAERSK))

    def test_env_can_widen_the_proxied_carriers(self):
        settings = self._settings(SCRAPER_PROXY_CARRIERS="MSC,HAPAG_LLOYD")
        self.assertEqual(settings.proxy_for(Carrier.MSC), "socks5://127.0.0.1:40000")
        self.assertEqual(settings.proxy_for(Carrier.HAPAG_LLOYD), "socks5://127.0.0.1:40000")
        self.assertIsNone(settings.proxy_for(Carrier.MAERSK))

    def test_all_disables_the_proxy_everywhere(self):
        # El workflow ya usa esto para depurar: SCRAPER_PROXY="".
        settings = self._settings(SCRAPER_PROXY="")
        self.assertIsNone(settings.proxy_for(Carrier.HAPAG_LLOYD))
        self.assertIsNone(settings.proxy_for(Carrier.MSC))

    def test_empty_carrier_list_means_direct_for_all(self):
        settings = self._settings(SCRAPER_PROXY_CARRIERS="")
        self.assertIsNone(settings.proxy_for(Carrier.HAPAG_LLOYD))

    def test_unknown_carrier_name_is_ignored(self):
        settings = self._settings(SCRAPER_PROXY_CARRIERS="NO_EXISTE,HAPAG_LLOYD")
        self.assertEqual(settings.proxy_for(Carrier.HAPAG_LLOYD), "socks5://127.0.0.1:40000")
        self.assertIsNone(settings.proxy_for(Carrier.MSC))

    def test_alternatives_never_cross_a_proxy_boundary(self):
        """Un contenedor ambiguo no puede probar Hapag desde el navegador directo.

        El proxy se fija al lanzar el navegador. Si el contenedor arranca en MSC
        (directo) y entre sus alternativas está Hapag (WARP), probar Hapag desde
        esa sesión sale sin proxy y el sitio responde 403: se gastaría un
        intento para recibir un bloqueo que no dice nada del contenedor.
        """
        settings = self._settings(SCRAPER_PROXY_CARRIERS="HAPAG_LLOYD")
        jobs, _ = build_plan(
            ["CAIU3430650"],
            (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD), 40,
        )
        self.assertEqual(jobs[0].parsed.carrier, Carrier.MSC)
        self.assertEqual(jobs[0].alternatives, [Carrier.MAERSK, Carrier.HAPAG_LLOYD])

        attempts = carrier_attempts(jobs[0], settings)
        self.assertEqual(attempts, [Carrier.MSC, Carrier.MAERSK])

    def test_alternatives_keep_proxied_carriers_together(self):
        # Si la naviera principal ya va por WARP, sus alternativas de WARP sí
        # valen: comparten sesión.
        settings = self._settings(SCRAPER_PROXY_CARRIERS="HAPAG_LLOYD,MSC")
        jobs, _ = build_plan(
            ["CAIU3430650"],
            (Carrier.HAPAG_LLOYD, Carrier.MSC, Carrier.MAERSK), 40,
        )
        self.assertEqual(jobs[0].parsed.carrier, Carrier.HAPAG_LLOYD)
        self.assertEqual(carrier_attempts(jobs[0], settings),
                         [Carrier.HAPAG_LLOYD, Carrier.MSC])

    def test_jobs_are_grouped_by_proxy_keeping_the_order(self):
        # Agrupar es lo que permite un solo navegador por configuración: el
        # proxy se fija al lanzarlo y no se puede cambiar por pestaña.
        settings = self._settings(SCRAPER_PROXY_CARRIERS="HAPAG_LLOYD")
        jobs, _ = build_plan(
            ["MSCU1234567", "HLBU2959982", "MRKU7779494"],
            (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD), 40,
        )
        groups = group_by_proxy(jobs, settings)
        self.assertEqual([c.value for c in groups[""]], ["MSC", "MAERSK"])
        self.assertEqual([c.value for c in groups["socks5://127.0.0.1:40000"]],
                         ["HAPAG_LLOYD"])


class ResultOrderTests(unittest.TestCase):
    """El reporte debe salir en el orden del plan, no en el de ejecución.

    Agrupar por proxy cambia el orden en que se consultan los contenedores
    (primero los directos, luego los de WARP). Sin reordenar, el JSON de salida
    cambiaría de orden según la configuración de proxy, y con él el diff del
    backend.
    """

    def _job(self, number, carrier):
        from scraper import Job
        from tracker.containers import parse

        return Job(parsed=parse(number, carrier_hint=carrier), index=0)

    def _result(self, number, status=ResultStatus.OK):
        from tracker.models import ContainerResult

        return ContainerResult(container_number=number, carrier="MSC", status=status.value)

    def test_results_follow_the_plan_order(self):
        jobs = [self._job("MSCU1234567", "MSC"), self._job("HLBU2959982", "HAPAG_LLOYD")]
        executed = [self._result("HLBU2959982"), self._result("MSCU1234567")]
        ordered = order_results(executed, jobs, pre_resolved=[])
        self.assertEqual([r.container_number for r in ordered],
                         ["MSCU1234567", "HLBU2959982"])

    def test_pre_resolved_go_first_and_appear_once(self):
        # Los descartados sin red ya venían resueltos: son parte del reporte y
        # no deben duplicarse al reordenar.
        jobs = [self._job("MSCU1234567", "MSC")]
        pre = [self._result("CAIU3430650", ResultStatus.INVALID)]
        executed = [self._result("MSCU1234567")]
        ordered = order_results(executed, jobs, pre_resolved=pre)
        self.assertEqual([r.container_number for r in ordered],
                         ["CAIU3430650", "MSCU1234567"])
        self.assertEqual(len(ordered), 2)

    def test_results_missing_from_the_plan_are_not_dropped(self):
        # Si un contenedor se consultó pero ya no está en el plan, se conserva
        # al final en vez de perderse.
        jobs = [self._job("MSCU1234567", "MSC")]
        executed = [self._result("ZZZZ7654321")]
        ordered = order_results(executed, jobs, pre_resolved=[])
        self.assertEqual([r.container_number for r in ordered], ["ZZZZ7654321"])

    def test_duplicate_numbers_are_collapsed(self):
        jobs = [self._job("MSCU1234567", "MSC")]
        executed = [self._result("MSCU1234567"), self._result("MSCU1234567")]
        ordered = order_results(executed, jobs, pre_resolved=[])
        self.assertEqual(len(ordered), 1)

    def test_empty_input_is_empty(self):
        self.assertEqual(order_results([], [], pre_resolved=[]), [])


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
