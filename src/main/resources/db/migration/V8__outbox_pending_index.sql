-- Keep polling and bounded claims proportional to pending work after delivered history grows.
CREATE INDEX outbox_pending ON business.outbox(id) WHERE NOT delivered;
