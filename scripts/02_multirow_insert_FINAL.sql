-- Method 02 (FINAL): 33 rows, NO NULLs, varchar(20)/(15)-safe, real app_user FK.
-- Verified against live Leather-Factory schema and SUCCESSFULLY LOADED.
-- FK-ordered, idempotent (ON CONFLICT DO NOTHING). Safe to re-run.
-- app_user id 1f643076-993c-4c6c-a659-14faf2e970a0 = "Direct Manager" (must pre-exist).
BEGIN;

INSERT INTO material_supplier (id, name, articles, contact, is_active) VALUES
  ('51ba3244-3465-5aef-9eab-a578b85b1ad0', 'GLOBEL', 'GOAT SUEDE,GOAT SUEDE WATER PROFF', 'globel@supp.example', TRUE),
  ('388e6196-362a-5a30-9eb2-ae6340e7513c', 'SAAD TANNING COMPANY', 'COW CALF,SHEEP NAPPA', 'saad@supp.example', TRUE),
  ('5fdea92b-c215-5356-b970-63e292be02fa', 'BISMI LEATHERS', 'SHEEP SPANISH,SHEEP MUST', 'bismi@supp.example', TRUE),
  ('cbe1258a-0709-5688-af73-233308c8c309', 'SHREE KRISHNA KNITS', 'RIBS,COTTON', 'krishna@supp.example', TRUE),
  ('33d5f13e-8883-5b7b-99dc-e07137687038', 'IMPORT-JERRY', 'ZIPS,RUNNER', 'jerry@supp.example', TRUE),
  ('8a436dd4-7f1b-537b-8634-de03a4a08100', 'BLACK ROCK EXPORTS', 'SHEEP DD,CHROME FREE SUEDE', 'blackrock@supp.example', TRUE),
  ('19a86aa4-ac45-5cc3-b85e-eba3bc93ed7b', 'LOCAL SHOP', 'ZIPS,BUTTONS', 'localshop@supplier.example', TRUE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO material_lot (id, category, subtype, article, colour, thickness, size, uom, on_hand, supplier_id, attributes, is_active) VALUES
  ('cf050b77-d46a-51a0-b714-a90ae01a7d6a', 'LEATHER', 'GENERAL', 'GOAT SUEDE', 'FOREST', '0.8-1.0', 'NA', 'DCM', 713.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{"pcs": 16, "buyer_ref": "JP"}'::jsonb, TRUE),
  ('175050c4-283b-55ab-9ecb-27178445dadb', 'LEATHER', 'GENERAL', 'GOAT SUEDE', 'D.BROWN', '0.8-1.0', 'NA', 'DCM', 174.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{"pcs": 6, "buyer_ref": "JP"}'::jsonb, TRUE),
  ('65c72b1e-3c32-5417-a4e9-7e6ded23905a', 'LEATHER', 'GENERAL', 'GOAT SUEDE', 'PINE GREEN', '0.8-1.0', 'NA', 'DCM', 890.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{"pcs": 16, "buyer_ref": "JP"}'::jsonb, TRUE),
  ('c0851f2f-bdd8-53e0-bbf6-c11d6824effd', 'LEATHER', 'GENERAL', 'COW CALF', 'WHISKY', '1.2-1.4', 'NA', 'DCM', 1250.500, '388e6196-362a-5a30-9eb2-ae6340e7513c', '{"pcs": 22}'::jsonb, TRUE),
  ('52342293-13ae-5bd2-8441-2265bf6fc868', 'LEATHER', 'GENERAL', 'SHEEP NAPPA', 'ICE', '0.6-0.8', 'NA', 'DCM', 430.250, '388e6196-362a-5a30-9eb2-ae6340e7513c', '{"pcs": 9}'::jsonb, TRUE),
  ('8f14ad62-f05b-594a-b679-94eb87392faf', 'LEATHER', 'GENERAL', 'SHEEP SPANISH', 'MILITARY', '0.7-0.9', 'NA', 'DCM', 560.000, '5fdea92b-c215-5356-b970-63e292be02fa', '{"pcs": 12}'::jsonb, TRUE),
  ('bd2e686e-a6ed-5931-a9de-bad344122d06', 'LEATHER', 'GENERAL', 'CHROME FREE SUEDE', 'VANILLA', '0.9-1.1', 'NA', 'DCM', 98.000, '8a436dd4-7f1b-537b-8634-de03a4a08100', '{"pcs": 3, "low_stock": true}'::jsonb, TRUE),
  ('a8f6c378-4ccc-5b56-b716-a32cb0ab2b9d', 'LINING', 'TAFFTA', 'TAFFTA LINING', 'BLACK', 'NA', 'NA', 'MTRS', 1200.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{"width_cm": 150}'::jsonb, TRUE),
  ('23390f49-bf8d-5a7b-8c4f-fd3a6c302463', 'LINING', 'COTTON', 'COTTON LINING', 'NATURAL', 'NA', 'NA', 'MTRS', 640.000, 'cbe1258a-0709-5688-af73-233308c8c309', '{"width_cm": 140}'::jsonb, TRUE),
  ('f3cfc9a0-9602-5973-9a71-d48e1a1ee337', 'LINING', 'RIBS', 'KNIT RIB', 'NAVY', 'NA', 'NA', 'MTRS', 300.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{}'::jsonb, TRUE),
  ('b0219d47-ce3e-5577-a9e6-41ca0e469e86', 'ACCESSORIES', 'ZIPS', 'METAL ZIP 5MM', 'ANTIQUE BRASS', 'NA', '60cm', 'PCS', 5000.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{"teeth": "metal"}'::jsonb, TRUE),
  ('d243407d-5e35-5015-9755-58b0f98cf49d', 'ACCESSORIES', 'ZIPS & RUNNER', 'RUNNER', 'GUNMETAL', 'NA', 'NA', 'PCS', 8000.000, '33d5f13e-8883-5b7b-99dc-e07137687038', '{}'::jsonb, TRUE),
  ('ef0215d5-dbcd-5561-b4de-f95f1a751b25', 'ACCESSORIES', 'THREADS', 'POLY THREAD 40s', 'BLACK', 'NA', 'NA', 'CONES', 420.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{"tex": 40}'::jsonb, TRUE),
  ('1d888072-3252-5ccd-92a2-9bcd142b80e7', 'ACCESSORIES', 'GENERAL', 'SNAP BUTTON 15MM', 'MATT BLACK', 'NA', '15mm', 'PCS', 12000.000, '51ba3244-3465-5aef-9eab-a578b85b1ad0', '{}'::jsonb, TRUE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO material_reservation (id, material_lot_id, qty, status, reason, released_at) VALUES
  ('537712c8-9b60-5cc2-8722-1d5774e9cd9d', 'cf050b77-d46a-51a0-b714-a90ae01a7d6a', 120.000, 'active', 'JP order cutting', '2099-12-31T00:00:00+00:00'),
  ('bbd084cf-d15f-5710-b290-7b7acc8b5cd9', 'c0851f2f-bdd8-53e0-bbf6-c11d6824effd', 300.000, 'active', 'Confezioni order', '2099-12-31T00:00:00+00:00'),
  ('899a0220-7d8d-54bf-9530-f03d93125b6f', '52342293-13ae-5bd2-8441-2265bf6fc868', 80.000, 'released', 'cancelled style', '2026-07-15T10:00:00+00:00'),
  ('3cc25184-09e4-51b8-b626-8e8b8fbc94cd', 'a8f6c378-4ccc-5b56-b716-a32cb0ab2b9d', 250.000, 'active', 'lining reserve', '2099-12-31T00:00:00+00:00')
ON CONFLICT (id) DO NOTHING;

INSERT INTO supplier_order (id, category, article, colour, qty, uom, status, supplier_id, ordered_by, arrived_at) VALUES
  ('f98179c6-7814-5e60-863b-0b9be53e8efb', 'LEATHER', 'GOAT SUEDE', 'FOREST', 500.000, 'DCM', 'arrived', '51ba3244-3465-5aef-9eab-a578b85b1ad0', '1f643076-993c-4c6c-a659-14faf2e970a0', '2026-07-20T09:00:00+00:00'),
  ('669735ee-2f34-5195-8776-f64429a668c7', 'LEATHER', 'COW CALF', 'WHISKY', 800.000, 'DCM', 'ordered', '388e6196-362a-5a30-9eb2-ae6340e7513c', '1f643076-993c-4c6c-a659-14faf2e970a0', '2099-12-31T00:00:00+00:00'),
  ('b5ecf067-d3e1-5111-93d9-fb455ac12bfa', 'LINING', 'TAFFTA LINING', 'BLACK', 1000.000, 'MTRS', 'arrived', '51ba3244-3465-5aef-9eab-a578b85b1ad0', '1f643076-993c-4c6c-a659-14faf2e970a0', '2026-07-20T09:00:00+00:00'),
  ('59942e33-f4cd-5a55-8789-680ef145be6d', 'ACCESSORIES', 'METAL ZIP 5MM', 'ANTIQUE BRASS', 3000.000, 'PCS', 'ordered', '51ba3244-3465-5aef-9eab-a578b85b1ad0', '1f643076-993c-4c6c-a659-14faf2e970a0', '2099-12-31T00:00:00+00:00'),
  ('2dc82df3-e4a4-51b3-9338-37d1a6fd0382', 'ACCESSORIES', 'POLY THREAD 40s', 'BLACK', 200.000, 'CONES', 'arrived', '51ba3244-3465-5aef-9eab-a578b85b1ad0', '1f643076-993c-4c6c-a659-14faf2e970a0', '2026-07-20T09:00:00+00:00')
ON CONFLICT (id) DO NOTHING;

INSERT INTO material_receipt (id, material_lot_id, supplier_order_id, approved_qty, rejected_qty, received_by) VALUES
  ('9ddc3643-03d2-5ae5-8fbb-6d390f2c3669', 'cf050b77-d46a-51a0-b714-a90ae01a7d6a', 'f98179c6-7814-5e60-863b-0b9be53e8efb', 480.000, 20.000, '1f643076-993c-4c6c-a659-14faf2e970a0'),
  ('dd185a7b-9bc9-5f6d-bb49-2f6e21ced535', 'a8f6c378-4ccc-5b56-b716-a32cb0ab2b9d', 'b5ecf067-d3e1-5111-93d9-fb455ac12bfa', 1000.000, 0.000, '1f643076-993c-4c6c-a659-14faf2e970a0'),
  ('3ce11f52-cac0-559e-8a34-48a25d7971af', 'ef0215d5-dbcd-5561-b4de-f95f1a751b25', '2dc82df3-e4a4-51b3-9338-37d1a6fd0382', 198.000, 2.000, '1f643076-993c-4c6c-a659-14faf2e970a0')
ON CONFLICT (id) DO NOTHING;

COMMIT;
