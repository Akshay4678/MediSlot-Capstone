-- MediSlot relational schema
-- MySQL 8.0+ / Amazon RDS MySQL 8.4
-- This schema follows the latest approved MediSlot Draw.io ER diagram.
--
-- Relational tables:
--   users, hospitals, specialisations, doctors, patients,
--   consultation_types, availability_patterns, blackout_dates,
--   appointments, refund_ledger.
--
-- DynamoDB entities shown in the ER diagram are provisioned separately:
--   TOKEN_ACTIVELIST, REFRESH_TOKENS, WAITLIST_ENTRIES,
--   IDEMPOTENCY_STORE, HOLD_OFFERS.
--
-- IMPORTANT:
--   The ER diagram uses BIGINT identifiers for RDS entities.
--   Do not mix this schema with older CHAR(36)/UUID relational schemas.
--   Back up and recreate the development database before applying it.

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT NOT NULL AUTO_INCREMENT,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('PATIENT', 'DOCTOR', 'ADMIN') NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email),
    KEY ix_users_role_active (role, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id BIGINT NOT NULL AUTO_INCREMENT,
    hospital_name VARCHAR(180) NOT NULL,
    address VARCHAR(500) NOT NULL,
    city VARCHAR(100) NOT NULL,
    phone VARCHAR(30) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (hospital_id),
    KEY ix_hospitals_city_active (city, is_active),
    KEY ix_hospitals_name (hospital_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id BIGINT NOT NULL AUTO_INCREMENT,
    specialisation_name VARCHAR(120) NOT NULL,
    description TEXT NULL,
    PRIMARY KEY (specialisation_id),
    UNIQUE KEY uq_specialisations_name (specialisation_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS patients (
    patient_id BIGINT NOT NULL AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    date_of_birth DATE NULL,
    gender VARCHAR(30) NULL,
    phone VARCHAR(30) NULL,
    address TEXT NULL,
    city VARCHAR(100) NULL,
    emergency_contact VARCHAR(30) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (patient_id),
    UNIQUE KEY uq_patients_user (user_id),
    KEY ix_patients_name (last_name, first_name),
    CONSTRAINT fk_patients_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id BIGINT NOT NULL AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    hospital_id BIGINT NOT NULL,
    specialisation_id BIGINT NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    licence_number VARCHAR(100) NOT NULL,
    experience_years INT NOT NULL DEFAULT 0,
    qualifications TEXT NULL,
    photo_url VARCHAR(1024) NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id),
    UNIQUE KEY uq_doctors_user (user_id),
    UNIQUE KEY uq_doctors_licence (licence_number),
    KEY ix_doctors_hospital_active (hospital_id, is_active),
    KEY ix_doctors_specialisation_active (specialisation_id, is_active),
    KEY ix_doctors_name (last_name, first_name),
    CONSTRAINT fk_doctors_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_doctors_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_doctors_specialisation
        FOREIGN KEY (specialisation_id) REFERENCES specialisations(specialisation_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_doctors_experience CHECK (experience_years >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Consultation types are a shared catalogue in the approved ER diagram.
-- There is intentionally NO doctor_id column in this table.
CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id BIGINT NOT NULL AUTO_INCREMENT,
    type_name VARCHAR(120) NOT NULL,
    duration_minutes INT NOT NULL,
    fee DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_type_name (type_name),
    CONSTRAINT chk_consultation_duration CHECK (duration_minutes > 0),
    CONSTRAINT chk_consultation_fee CHECK (fee >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS availability_patterns (
    availability_id BIGINT NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT NOT NULL,
    consultation_type_id BIGINT NOT NULL,
    day_of_week INT NOT NULL COMMENT 'ISO weekday: Monday=1 through Sunday=7',
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    capacity INT NOT NULL DEFAULT 1,
    PRIMARY KEY (availability_id),
    KEY ix_availability_doctor_day_active (doctor_id, day_of_week, is_active),
    KEY ix_availability_consultation_type (consultation_type_id),
    CONSTRAINT fk_availability_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_availability_consultation_type
        FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_availability_weekday CHECK (day_of_week BETWEEN 1 AND 7),
    CONSTRAINT chk_availability_time CHECK (end_time > start_time),
    CONSTRAINT chk_availability_capacity CHECK (capacity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id BIGINT NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT NOT NULL,
    blackout_date DATE NOT NULL,
    reason VARCHAR(255) NULL,
    PRIMARY KEY (blackout_id),
    KEY ix_blackout_doctor_date (doctor_id, blackout_date),
    CONSTRAINT fk_blackout_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS materialised_slots (
    slot_id VARCHAR(200) NOT NULL,
    availability_id BIGINT NOT NULL,
    doctor_id BIGINT NOT NULL,
    consultation_type_id BIGINT NOT NULL,
    `date` DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    capacity INT NOT NULL DEFAULT 1,
    booked_count INT NOT NULL DEFAULT 0,
    status ENUM('AVAILABLE', 'FULL', 'UNAVAILABLE') NOT NULL DEFAULT 'AVAILABLE',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (slot_id),
    UNIQUE KEY uq_materialised_slot_pattern_start (availability_id, `date`, start_time),
    KEY ix_materialised_slots_doctor_date (doctor_id, `date`, start_time),
    KEY ix_materialised_slots_availability_date (availability_id, `date`),
    CONSTRAINT fk_materialised_slots_availability
        FOREIGN KEY (availability_id) REFERENCES availability_patterns(availability_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_materialised_slots_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_materialised_slots_consultation_type
        FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_materialised_slots_capacity CHECK (capacity > 0),
    CONSTRAINT chk_materialised_slots_booked CHECK (booked_count >= 0 AND booked_count <= capacity),
    CONSTRAINT chk_materialised_slots_time CHECK (end_time > start_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id BIGINT NOT NULL AUTO_INCREMENT,
    patient_id BIGINT NOT NULL,
    doctor_id BIGINT NOT NULL,
    slot_id VARCHAR(200) NOT NULL,
    consultation_type_id BIGINT NOT NULL,
    status ENUM(
        'PENDING',
        'CONFIRMED',
        'CANCELLED',
        'COMPLETED',
        'NO_SHOW',
        'RESCHEDULED'
    ) NOT NULL DEFAULT 'PENDING',
    notes TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (appointment_id),
    KEY ix_appointments_patient (patient_id),
    KEY ix_appointments_doctor (doctor_id),
    KEY ix_appointments_doctor_status (doctor_id, status),
    KEY ix_appointments_slot (slot_id),
    KEY ix_appointments_consultation_type (consultation_type_id),
    CONSTRAINT fk_appointments_patient
        FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_appointments_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_appointments_consultation_type
        FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_appointments_slot
        FOREIGN KEY (slot_id) REFERENCES materialised_slots(slot_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id BIGINT NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT NOT NULL,
    refund_amount DECIMAL(10, 2) NOT NULL,
    refund_percentage DECIMAL(5, 2) NOT NULL,
    refund_status ENUM('PENDING', 'PROCESSED', 'FAILED', 'NOT_APPLICABLE')
        NOT NULL DEFAULT 'PENDING',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refund_appointment (appointment_id),
    CONSTRAINT fk_refund_appointment
        FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_refund_amount CHECK (refund_amount >= 0),
    CONSTRAINT chk_refund_percentage CHECK (refund_percentage BETWEEN 0 AND 100)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- Seed catalogue rows for a fresh development database.
-- These statements are safe to run repeatedly.
-- ---------------------------------------------------------------------------

INSERT INTO hospitals (hospital_name, address, city, phone, is_active)
SELECT 'Lakeview General Hospital', '12 Lakeview Road, Jubilee Hills', 'Hyderabad', '+91-4000000001', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM hospitals WHERE hospital_name = 'Lakeview General Hospital'
);

INSERT INTO hospitals (hospital_name, address, city, phone, is_active)
SELECT 'Sunrise Care Clinic', '45 Park Avenue, Banjara Hills', 'Hyderabad', '+91-4000000002', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM hospitals WHERE hospital_name = 'Sunrise Care Clinic'
);

INSERT INTO hospitals (hospital_name, address, city, phone, is_active)
SELECT 'Greenfield Medical Centre', '8 Garden Street, Indiranagar', 'Bengaluru', '+91-8000000003', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM hospitals WHERE hospital_name = 'Greenfield Medical Centre'
);

INSERT INTO specialisations (specialisation_name, description)
SELECT 'Cardiology', 'Heart and cardiovascular care'
WHERE NOT EXISTS (
    SELECT 1 FROM specialisations WHERE specialisation_name = 'Cardiology'
);

INSERT INTO specialisations (specialisation_name, description)
SELECT 'General Medicine', 'Primary care and general health'
WHERE NOT EXISTS (
    SELECT 1 FROM specialisations WHERE specialisation_name = 'General Medicine'
);

INSERT INTO specialisations (specialisation_name, description)
SELECT 'Dermatology', 'Skin, hair and nail care'
WHERE NOT EXISTS (
    SELECT 1 FROM specialisations WHERE specialisation_name = 'Dermatology'
);

INSERT INTO specialisations (specialisation_name, description)
SELECT 'Paediatrics', 'Care for infants and children'
WHERE NOT EXISTS (
    SELECT 1 FROM specialisations WHERE specialisation_name = 'Paediatrics'
);

INSERT INTO specialisations (specialisation_name, description)
SELECT 'Orthopaedics', 'Bones, joints and muscles'
WHERE NOT EXISTS (
    SELECT 1 FROM specialisations WHERE specialisation_name = 'Orthopaedics'
);

INSERT INTO consultation_types (type_name, duration_minutes, fee)
SELECT 'Quick Consult', 15, 400.00
WHERE NOT EXISTS (
    SELECT 1 FROM consultation_types WHERE type_name = 'Quick Consult'
);

INSERT INTO consultation_types (type_name, duration_minutes, fee)
SELECT 'Standard Consult', 30, 800.00
WHERE NOT EXISTS (
    SELECT 1 FROM consultation_types WHERE type_name = 'Standard Consult'
);

INSERT INTO consultation_types (type_name, duration_minutes, fee)
SELECT 'Detailed Consult', 60, 1400.00
WHERE NOT EXISTS (
    SELECT 1 FROM consultation_types WHERE type_name = 'Detailed Consult'
);

INSERT INTO consultation_types (type_name, duration_minutes, fee)
SELECT 'Health Camp Session', 60, 200.00
WHERE NOT EXISTS (
    SELECT 1 FROM consultation_types WHERE type_name = 'Health Camp Session'
);

-- ---------------------------------------------------------------------------
-- Implementation notes
-- ---------------------------------------------------------------------------
-- 1. RDS identifiers are generated by AUTO_INCREMENT; do not insert UUID strings.
-- 2. The application must obtain user_id/doctor_id/patient_id from lastrowid.
-- 3. Passwords must be bcrypt hashes; never store plaintext passwords.
-- 4. Store materialised slots, capacity and booked_count in RDS materialised_slots.
-- 5. appointments.slot_id is a foreign key to materialised_slots.slot_id.
-- 6. availability_patterns use the doctor's IANA timezone from doctors.timezone.
-- 7. Materialise local availability using the doctor timezone and retain local
--    date and wall-clock start/end values in RDS materialised_slots.
-- 8. Use parameterised SQL and transactions for all multi-row writes.
