import json

from tracker.containers import check_digit_valid, compute_check_digit, parse, split_csv
from tracker.extract import extract

print("checkdigit MSCU1234567 ->", compute_check_digit("MSCU1234567"), "valid:", check_digit_valid("MSCU1234567"))
print(split_csv(" mscu1234567, MAEU 765 432 1 ;HLCU1111111,mscu1234567"))
print(parse("mscu1234567"))

payload = {
    "trackingNumber": "MSCU1234567",
    "shipment": {
        "events": [
            {"eventDate": "2026-11-01T10:00:00Z", "eventName": "Gate Out", "locationName": "Callao",
             "vesselName": "MSC PERU", "voyageNumber": "123E"},
            {"eventDate": "2026-11-05T08:30:00Z", "eventName": "Loaded", "locationName": "Callao",
             "vesselName": "MSC PERU", "voyageNumber": "123E"},
            {"eventDate": "2026-11-12T03:00:00Z", "eventName": "Discharged", "locationName": "Shanghai"},
        ],
        "eta": "2026-11-30T00:00:00Z",
    },
}
out = extract(payload, "MSCU1234567")
print("events", out.event_count, "| status", out.data.current_status, "| eta", out.data.eta, "| loc", out.data.location)
print(json.dumps(out.data.to_dict(), indent=2, ensure_ascii=False)[:400])

print("notfound:", extract({"error": "Container not found"}, "MSCU1234567").not_found)
print("blocked:", extract({"message": "Access Denied Reference #18.abc"}, "MSCU1234567").blocked)

maersk = {"data": {"shipment": {"containers": [{"containerNumber": "MRKU1234567"}]},
                   "events": [{"eventType": "GATE_IN", "eventTime": "2026-10-02T12:00:00.000+0000",
                               "location": {"city": "Lima", "country": "PE"},
                               "vessel": {"name": "MAERSK LIMA"}}]}}
m = extract(maersk, "MRKU1234567")
print("maersk:", m.event_count, m.data.current_status, m.data.location, m.data.vessel, m.data.last_update)

hl = {"shipmentEvents": [{"description": "Empty to shipper", "eventDateTime": 1762000000,
                          "locationName": "Callao"}, {"description": "Vessel departure",
                          "eventDateTime": 1762300000, "locationName": "Callao"}]}
h = extract(hl, "HLCU1111111")
print("hl:", h.event_count, h.data.current_status, h.data.last_update)
