# TrackLine Telematics: account notes for Maple Freight

Copied from the TrackLine support portal by Maple Freight IT, September 2026.
(TrackLine is fictional.)

## About your alert export

- **Timestamps.** `event_time_utc` is when the device recorded the event. `received_time_utc` is when it reached our platform. Both are in UTC.
- **Severity.** `vendor_severity` is assigned automatically by our risk engine. For engine faults it follows the urgency of the diagnostic trouble code. For speeding it follows how far the vehicle was over the posted limit.
- **Delivery.** The platform delivers each alert exactly once. Every `alert_id` represents a unique event.
- **Posted limits.** These come from our map provider. Coverage can be incomplete on some roads.
- **Connectivity.** All Maple Freight depots report normal gateway connectivity. No open incidents.

## Device firmware

- **4.3.0** (current): stability fixes.
- **4.2.1** (August 2026): improved harsh-event sensitivity. It detects more risky braking that earlier firmware missed. We recommend it for safety programs.
- **4.1.8**: legacy. Still supported.

## Device behaviour

- **Nightly updates.** Devices download over-the-air updates in a nightly maintenance window. During it you may see `device_unplugged` and `camera_offline` alerts between 23:00 and 00:00 local time. These are expected and can be safely ignored.
- **Maintenance alerts.** `maintenance_due` alerts are generated once a day at 06:00 local. A negative value means the vehicle is not yet due.
