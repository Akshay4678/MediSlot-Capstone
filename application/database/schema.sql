-- MediSlot MySQL schema for MySQL 8.0+ / Amazon RDS MySQL 8.4
-- Run this against the database created by rds.yaml (default DBName: medislot).
-- DynamoDB-backed slot capacity, idempotency, waitlist entries, hold offers,
-- refresh/revocation state, and notification dispatch records are NOT created here.
-- They are separate DynamoDB tables defined by the data CloudFormation stack.

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS users (
    user_id CHAR(36) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('PATIENT','DOCTOR','ADMIN') NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email),
    KEY ix_users_role_active (role, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id CHAR(36) NOT NULL,
    name VARCHAR(180) NOT NULL,
    address_line1 VARCHAR(200) NOT NULL,
    address_line2 VARCHAR(200) NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100) NULL,
    postal_code VARCHAR(20) NULL,
    country VARCHAR(100) NOT NULL DEFAULT 'India',
    phone VARCHAR(30) NULL,
    email VARCHAR(254) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (hospital_id),
    KEY ix_hospitals_city_active (city, is_active),
    KEY ix_hospitals_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id CHAR(36) NOT NULL,
    name VARCHAR(120) NOT NULL,
    description VARCHAR(500) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (specialisation_id),
    UNIQUE KEY uq_specialisations_name (name),
    KEY ix_specialisations_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS patients (
    patient_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    date_of_birth DATE NULL,
    phone VARCHAR(30) NULL,
    gender ENUM('FEMALE','MALE','OTHER','UNDISCLOSED') NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (patient_id),
    UNIQUE KEY uq_patients_user (user_id),
    KEY ix_patients_name (last_name, first_name),
    CONSTRAINT fk_patients_user FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    hospital_id CHAR(36) NOT NULL,
    specialisation_id CHAR(36) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    display_name VARCHAR(220) GENERATED ALWAYS AS (CONCAT('Dr. ', first_name, ' ', last_name)) STORED,
    qualifications VARCHAR(500) NULL,
    years_experience SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    bio TEXT NULL,
    phone VARCHAR(30) NULL,
    consultation_fee DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (doctor_id),
    UNIQUE KEY uq_doctors_user (user_id),
    KEY ix_doctors_hospital_active (hospital_id, is_active),
    KEY ix_doctors_specialisation_active (specialisation_id, is_active),
    KEY ix_doctors_name (last_name, first_name),
    CONSTRAINT fk_doctors_user FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_doctors_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_doctors_specialisation FOREIGN KEY (specialisation_id) REFERENCES specialisations(specialisation_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_doctors_fee CHECK (consultation_fee >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    name VARCHAR(120) NOT NULL,
    description VARCHAR(500) NULL,
    duration_minutes SMALLINT UNSIGNED NOT NULL,
    fee DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_type_doctor_name (doctor_id, name),
    KEY ix_consultation_types_doctor_active (doctor_id, is_active),
    CONSTRAINT fk_consultation_types_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_consultation_duration CHECK (duration_minutes > 0),
    CONSTRAINT chk_consultation_fee CHECK (fee >= 0),
    CONSTRAINT chk_consultation_capacity CHECK (capacity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS availability_patterns (
    availability_pattern_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    weekday TINYINT UNSIGNED NOT NULL COMMENT 'ISO weekday: Monday=1 through Sunday=7',
    start_local_time TIME NOT NULL,
    end_local_time TIME NOT NULL,
    timezone_name VARCHAR(64) NOT NULL COMMENT 'IANA timezone, e.g. Asia/Kolkata',
    effective_from DATE NOT NULL,
    effective_until DATE NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (availability_pattern_id),
    KEY ix_availability_doctor_weekday_active (doctor_id, weekday, active),
    KEY ix_availability_consultation_type (consultation_type_id),
    CONSTRAINT fk_availability_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_availability_consultation_type FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_availability_weekday CHECK (weekday BETWEEN 1 AND 7),
    CONSTRAINT chk_availability_time CHECK (end_local_time > start_local_time),
    CONSTRAINT chk_availability_dates CHECK (effective_until IS NULL OR effective_until >= effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    local_date DATE NOT NULL,
    start_local_time TIME NULL COMMENT 'NULL means the entire local date is blocked',
    end_local_time TIME NULL,
    reason VARCHAR(300) NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (blackout_id),
    KEY ix_blackout_doctor_date (doctor_id, local_date),
    CONSTRAINT fk_blackout_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_blackout_time CHECK (
        (start_local_time IS NULL AND end_local_time IS NULL) OR
        (start_local_time IS NOT NULL AND end_local_time IS NOT NULL AND end_local_time > start_local_time)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id CHAR(36) NOT NULL,
    patient_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    slot_id VARCHAR(160) NOT NULL COMMENT 'References DynamoDB slot-capacity item; not an RDS foreign key',
    starts_at_utc DATETIME(6) NOT NULL COMMENT 'UTC timestamp',
    ends_at_utc DATETIME(6) NOT NULL COMMENT 'UTC timestamp',
    timezone_name VARCHAR(64) NOT NULL,
    status ENUM('PENDING','CONFIRMED','CANCELLED','COMPLETED','NO_SHOW','RESCHEDULED','FAILED') NOT NULL DEFAULT 'PENDING',
    cancellation_reason VARCHAR(500) NULL,
    reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    idempotency_key VARCHAR(255) NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (appointment_id),
    UNIQUE KEY uq_appointments_idempotency (idempotency_key),
    KEY ix_appointments_patient_start_status (patient_id, starts_at_utc, status),
    KEY ix_appointments_doctor_start_status (doctor_id, starts_at_utc, status),
    KEY ix_appointments_slot (slot_id),
    KEY ix_appointments_status_start (status, starts_at_utc),
    CONSTRAINT fk_appointments_patient FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_appointments_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_appointments_consultation_type FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_appointments_time CHECK (ends_at_utc > starts_at_utc),
    CONSTRAINT chk_appointments_reschedule_count CHECK (reschedule_count <= 2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id CHAR(36) NOT NULL,
    appointment_id CHAR(36) NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'INR',
    refund_percentage DECIMAL(5,2) NOT NULL,
    reason ENUM('PATIENT_CANCELLATION','DOCTOR_CANCELLATION','NO_SHOW','ADMIN_ADJUSTMENT') NOT NULL,
    status ENUM('CALCULATED','PENDING','PROCESSED','FAILED','NOT_APPLICABLE') NOT NULL DEFAULT 'CALCULATED',
    policy_version VARCHAR(40) NOT NULL DEFAULT 'v1',
    idempotency_key VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refund_appointment (appointment_id),
    UNIQUE KEY uq_refund_idempotency (idempotency_key),
    KEY ix_refund_status_created (status, created_at),
    CONSTRAINT fk_refund_appointment FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_refund_amount CHECK (amount >= 0),
    CONSTRAINT chk_refund_percentage CHECK (refund_percentage >= 0 AND refund_percentage <= 100)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Never insert accounts with plaintext passwords: seed accounts below use bcrypt hashes only.

-- =====================================================================
-- DEVELOPMENT SEED DATA (idempotent: safe to re-run on every deploy)
-- * Existing rows are never overwritten, so later profile edits survive.
-- * Parent rows are looked up by natural key (email / specialisation name),
--   NOT by assumed IDs, so the seed also works if an account such as
--   doctor1@example.com was already registered through the API.
-- Sample doctor accounts: doctor1@example.com ... doctor4@example.com
-- Passwords are stored ONLY as bcrypt hashes (work factor 12).
-- For a production environment, move this block to a dev-only file.
-- =====================================================================

INSERT INTO hospitals (hospital_id, name, address_line1, city, state, postal_code, country) VALUES
    ('552ff03b-e90c-5672-91e7-78cd42654538', 'Lakeview General Hospital', '12 Lakeview Road, Jubilee Hills', 'Hyderabad', 'Telangana', '500033', 'India'),
    ('7d2b0c11-2506-58d5-a845-1cfcbd8b64e0', 'Sunrise Care Clinic', '45 Park Avenue, Banjara Hills', 'Hyderabad', 'Telangana', '500034', 'India'),
    ('5f39b269-c16d-5b6d-838e-c65672002651', 'Greenfield Medical Centre', '8 Garden Street, Indiranagar', 'Bengaluru', 'Karnataka', '560038', 'India')
ON DUPLICATE KEY UPDATE hospital_id = hospital_id;

INSERT INTO specialisations (specialisation_id, name, description) VALUES
    ('ad359f48-c0c7-51ea-8063-61773f0eacbd', 'Cardiology', 'Heart and cardiovascular care'),
    ('f0db8612-077c-57be-9c68-f1981fc5c99c', 'General Medicine', 'Primary care and general health'),
    ('a0c52501-cb94-5716-bfe2-d60bd58e0636', 'Dermatology', 'Skin, hair and nail care'),
    ('ce73354d-4129-5790-b12e-14d25c0bd183', 'Paediatrics', 'Care for infants and children'),
    ('e54e5198-0dc5-5afa-b631-d888c7a531f5', 'Orthopaedics', 'Bones, joints and muscles')
ON DUPLICATE KEY UPDATE specialisation_id = specialisation_id;

INSERT INTO users (user_id, email, password_hash, role, is_active, email_verified) VALUES
    ('3cda2790-fffc-5400-b9a1-c2236d0250d8', 'doctor1@example.com', '$2b$12$weCHDUG/EgIMFzVhMoZC9enTBeIkUk4zTuzA6rw.n.GCqW0/6f8zS', 'DOCTOR', TRUE, TRUE),
    ('d39248c4-f567-58c8-acf7-b23e595a09f6', 'doctor2@example.com', '$2b$12$n08c3eYjy1Rv9cH/wbWqpOjn9bP5uNi3SKnODWeybFnwVvDnmJ6yK', 'DOCTOR', TRUE, TRUE),
    ('873a8bdb-ece4-51cc-9d30-8b499f7ddef2', 'doctor3@example.com', '$2b$12$zNXXkINMJu1u5fDfKjksmOdyc7hGvF0UFHGw2vpqb3HxCB4cAPysi', 'DOCTOR', TRUE, TRUE),
    ('67f59346-9788-5841-b5af-03726468434a', 'doctor4@example.com', '$2b$12$6cxj2Eyr09WIeSbHRY39EO3s3DkT9zJQ3a9CQUFmqabKsXRTtMPv2', 'DOCTOR', TRUE, TRUE)
ON DUPLICATE KEY UPDATE user_id = user_id;

-- Doctor profile for doctor1@example.com (user_id and specialisation_id are looked up)
INSERT INTO doctors (doctor_id, user_id, hospital_id, specialisation_id, first_name, last_name, qualifications, years_experience, phone, consultation_fee)
SELECT '98b99efa-fc4f-5a0b-ac7d-8c7b5a54c853', u.user_id, '552ff03b-e90c-5672-91e7-78cd42654538', s.specialisation_id, 'Priya', 'Sharma', 'MBBS, MD (Cardiology)', 12, '+91-9000000001', 800
FROM users u
JOIN specialisations s ON s.name = 'Cardiology'
WHERE u.email = 'doctor1@example.com' AND u.role = 'DOCTOR'
ON DUPLICATE KEY UPDATE doctor_id = doctor_id;

-- Doctor profile for doctor2@example.com (user_id and specialisation_id are looked up)
INSERT INTO doctors (doctor_id, user_id, hospital_id, specialisation_id, first_name, last_name, qualifications, years_experience, phone, consultation_fee)
SELECT 'b43a84f3-b681-5b56-8537-9ec860c263c9', u.user_id, '7d2b0c11-2506-58d5-a845-1cfcbd8b64e0', s.specialisation_id, 'Rahul', 'Verma', 'MBBS, MD (General Medicine)', 8, '+91-9000000002', 500
FROM users u
JOIN specialisations s ON s.name = 'General Medicine'
WHERE u.email = 'doctor2@example.com' AND u.role = 'DOCTOR'
ON DUPLICATE KEY UPDATE doctor_id = doctor_id;

-- Doctor profile for doctor3@example.com (user_id and specialisation_id are looked up)
INSERT INTO doctors (doctor_id, user_id, hospital_id, specialisation_id, first_name, last_name, qualifications, years_experience, phone, consultation_fee)
SELECT '60c82fb1-e883-5986-99ce-bc424b954ed4', u.user_id, '5f39b269-c16d-5b6d-838e-c65672002651', s.specialisation_id, 'Ananya', 'Reddy', 'MBBS, MD (Dermatology)', 6, '+91-9000000003', 600
FROM users u
JOIN specialisations s ON s.name = 'Dermatology'
WHERE u.email = 'doctor3@example.com' AND u.role = 'DOCTOR'
ON DUPLICATE KEY UPDATE doctor_id = doctor_id;

-- Doctor profile for doctor4@example.com (user_id and specialisation_id are looked up)
INSERT INTO doctors (doctor_id, user_id, hospital_id, specialisation_id, first_name, last_name, qualifications, years_experience, phone, consultation_fee)
SELECT '3151f055-eeb6-5a1c-84d6-73514264c628', u.user_id, '552ff03b-e90c-5672-91e7-78cd42654538', s.specialisation_id, 'Karthik', 'Rao', 'MBBS, DCH, MD (Paediatrics)', 15, '+91-9000000004', 700
FROM users u
JOIN specialisations s ON s.name = 'Paediatrics'
WHERE u.email = 'doctor4@example.com' AND u.role = 'DOCTOR'
ON DUPLICATE KEY UPDATE doctor_id = doctor_id;

-- Consultation types for doctor1@example.com (doctor_id is looked up via the account email)
INSERT INTO consultation_types (consultation_type_id, doctor_id, name, description, duration_minutes, fee, capacity)
SELECT t.ct_id, dr.doctor_id, t.ct_name, t.ct_desc, t.ct_minutes, t.ct_fee, t.ct_capacity
FROM (
    SELECT 'c46b6593-3879-58a0-b758-c276c3225e90' AS ct_id, 'Quick Consult' AS ct_name, 'Short follow-up or simple query' AS ct_desc, 15 AS ct_minutes, 400 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT 'cbadb35a-17d1-50ec-ad09-39cdd10d8bc4' AS ct_id, 'Standard Consult' AS ct_name, 'Regular consultation' AS ct_desc, 30 AS ct_minutes, 800 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT '39a19693-b820-53cb-ae1c-fd75a4c0b58b' AS ct_id, 'Detailed Consult' AS ct_name, 'Extended in-depth consultation' AS ct_desc, 60 AS ct_minutes, 1400 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT 'aa9f22d7-afaf-5694-910f-0480623c5cec' AS ct_id, 'Health Camp Session' AS ct_name, 'Group heart-health screening (3 seats)' AS ct_desc, 60 AS ct_minutes, 200 AS ct_fee, 3 AS ct_capacity
) t
JOIN users u ON u.email = 'doctor1@example.com' AND u.role = 'DOCTOR'
JOIN doctors dr ON dr.user_id = u.user_id
ON DUPLICATE KEY UPDATE consultation_type_id = consultation_type_id;

-- Consultation types for doctor2@example.com (doctor_id is looked up via the account email)
INSERT INTO consultation_types (consultation_type_id, doctor_id, name, description, duration_minutes, fee, capacity)
SELECT t.ct_id, dr.doctor_id, t.ct_name, t.ct_desc, t.ct_minutes, t.ct_fee, t.ct_capacity
FROM (
    SELECT '0081bc9a-9c66-5478-8c7b-dff782d217a3' AS ct_id, 'Quick Consult' AS ct_name, 'Short follow-up or simple query' AS ct_desc, 15 AS ct_minutes, 250 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT 'cfbd50af-b268-5891-8d08-bb0d8378df87' AS ct_id, 'Standard Consult' AS ct_name, 'Regular consultation' AS ct_desc, 30 AS ct_minutes, 500 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT 'fe953ada-6388-5f98-84b4-c4c5ec162c2a' AS ct_id, 'Detailed Consult' AS ct_name, 'Extended in-depth consultation' AS ct_desc, 60 AS ct_minutes, 900 AS ct_fee, 1 AS ct_capacity
) t
JOIN users u ON u.email = 'doctor2@example.com' AND u.role = 'DOCTOR'
JOIN doctors dr ON dr.user_id = u.user_id
ON DUPLICATE KEY UPDATE consultation_type_id = consultation_type_id;

-- Consultation types for doctor3@example.com (doctor_id is looked up via the account email)
INSERT INTO consultation_types (consultation_type_id, doctor_id, name, description, duration_minutes, fee, capacity)
SELECT t.ct_id, dr.doctor_id, t.ct_name, t.ct_desc, t.ct_minutes, t.ct_fee, t.ct_capacity
FROM (
    SELECT '391e3ca7-2dbe-5a52-a05c-6dd340fed7ca' AS ct_id, 'Quick Consult' AS ct_name, 'Short follow-up or simple query' AS ct_desc, 15 AS ct_minutes, 300 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT 'a36d8b07-d284-570e-a3bf-5b4386da4eef' AS ct_id, 'Standard Consult' AS ct_name, 'Regular consultation' AS ct_desc, 30 AS ct_minutes, 600 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT '5794fc04-5499-5778-a737-09f717f864c3' AS ct_id, 'Detailed Consult' AS ct_name, 'Extended in-depth consultation' AS ct_desc, 60 AS ct_minutes, 1000 AS ct_fee, 1 AS ct_capacity
) t
JOIN users u ON u.email = 'doctor3@example.com' AND u.role = 'DOCTOR'
JOIN doctors dr ON dr.user_id = u.user_id
ON DUPLICATE KEY UPDATE consultation_type_id = consultation_type_id;

-- Consultation types for doctor4@example.com (doctor_id is looked up via the account email)
INSERT INTO consultation_types (consultation_type_id, doctor_id, name, description, duration_minutes, fee, capacity)
SELECT t.ct_id, dr.doctor_id, t.ct_name, t.ct_desc, t.ct_minutes, t.ct_fee, t.ct_capacity
FROM (
    SELECT '0241db69-4bb1-5437-abf8-20123d1b3719' AS ct_id, 'Quick Consult' AS ct_name, 'Short follow-up or simple query' AS ct_desc, 15 AS ct_minutes, 350 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT '47b0ccb7-ca20-5c40-b56d-eca7042abee7' AS ct_id, 'Standard Consult' AS ct_name, 'Regular consultation' AS ct_desc, 30 AS ct_minutes, 700 AS ct_fee, 1 AS ct_capacity
    UNION ALL SELECT '364507d5-efa6-5237-a3d5-6d61eb9a67c2' AS ct_id, 'Detailed Consult' AS ct_name, 'Extended in-depth consultation' AS ct_desc, 60 AS ct_minutes, 1200 AS ct_fee, 1 AS ct_capacity
) t
JOIN users u ON u.email = 'doctor4@example.com' AND u.role = 'DOCTOR'
JOIN doctors dr ON dr.user_id = u.user_id
ON DUPLICATE KEY UPDATE consultation_type_id = consultation_type_id;

-- Important implementation notes:
-- 1) Generate CHAR(36) IDs in the application (UUIDs) before INSERT.
-- 2) Hash passwords with a suitable password-hashing algorithm (e.g. bcrypt); never store plaintext.
-- 3) Store all appointment instants in UTC; convert to/from the doctor's IANA timezone in application code.
-- 4) Use parameterized SQL and explicit transactions for multi-table writes.
-- 5) DynamoDB slot-capacity conditional updates and RDS appointment writes are not one ACID transaction.
--    Implement idempotency and compensation/reconciliation in the booking workflow.
-- 6) If your existing handlers use different table/column names, update them to match this schema before deployment.