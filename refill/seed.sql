INSERT OR IGNORE INTO pharmacies (id, name, address) VALUES
    (1, 'CVS Pharmacy', '1234 Main St'),
    (2, 'Walgreens', '500 Oak Ave'),
    (3, 'Walmart Pharmacy', '88 Market St');

INSERT OR IGNORE INTO patients (id, full_name, date_of_birth, phone, pharmacy_id, insurance_plan, member_id) VALUES
    (1, 'Sarah Chen', '1985-03-15', '(555) 867-5309', 1, 'Aetna PPO', 'ANT-88912');

INSERT OR IGNORE INTO prescriptions (id, patient_id, medication, dosage, frequency, indication) VALUES
    (1, 1, 'Lisinopril', '10mg', 'Once daily', 'blood pressure'),
    (2, 1, 'Metformin', '500mg', 'Twice daily', 'diabetes'),
    (3, 1, 'Atorvastatin', '20mg', 'Once daily', 'cholesterol');

INSERT OR IGNORE INTO copays (insurance_plan, medication, amount_cents) VALUES
    ('Aetna PPO', 'Lisinopril', 500),
    ('Aetna PPO', 'Metformin', 1000),
    ('Aetna PPO', 'Atorvastatin', 1500);
