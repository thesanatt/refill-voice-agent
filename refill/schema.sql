CREATE TABLE IF NOT EXISTS pharmacies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    pharmacy_id INTEGER NOT NULL REFERENCES pharmacies(id),
    insurance_plan TEXT NOT NULL,
    member_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prescriptions (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    medication TEXT NOT NULL,
    dosage TEXT NOT NULL,
    frequency TEXT NOT NULL,
    indication TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS copays (
    insurance_plan TEXT NOT NULL,
    medication TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (insurance_plan, medication)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('voice', 'sms')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'dropped', 'completed', 'declined', 'handed_off')),
    caller_phone TEXT NOT NULL,
    patient_id INTEGER REFERENCES patients(id),
    verified_at TEXT,
    identity_failures INTEGER NOT NULL DEFAULT 0,
    prescription_id INTEGER REFERENCES prescriptions(id),
    pharmacy_id INTEGER REFERENCES pharmacies(id),
    pharmacy_text TEXT,
    insurance_status TEXT CHECK (insurance_status IN ('current', 'changed')),
    price_stated_at TEXT,
    needs_pharmacist INTEGER NOT NULL DEFAULT 0,
    pharmacist_reason TEXT,
    unclear_turns INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refill_requests (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
    medication TEXT NOT NULL,
    dosage TEXT NOT NULL,
    pharmacy TEXT NOT NULL,
    insurance_plan TEXT NOT NULL,
    member_id TEXT NOT NULL,
    estimated_copay_cents INTEGER,
    needs_pharmacist INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'submitted',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    channel TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('patient', 'agent')),
    text TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_log (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    step TEXT NOT NULL,
    tools TEXT NOT NULL,
    llm_ms INTEGER,
    total_ms INTEGER NOT NULL,
    first_audio_ms INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL
);
