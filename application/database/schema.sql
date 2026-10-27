
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

-- Optional seed catalogue: add real hospital/doctor records through an admin workflow.
-- Do not insert sample patient or doctor accounts with plaintext passwords.

-- Important implementation notes:
-- 1) Generate CHAR(36) IDs in the application (UUIDs) before INSERT.
-- 2) Hash passwords with a suitable password-hashing algorithm (e.g. bcrypt); never store plaintext.
-- 3) Store all appointment instants in UTC; convert to/from the doctor's IANA timezone in application code.
-- 4) Use parameterized SQL and explicit transactions for multi-table writes.
-- 5) DynamoDB slot-capacity conditional updates and RDS appointment writes are not one ACID transaction.
--    Implement idempotency and compensation/reconciliation in the booking workflow.
-- 6) If your existing handlers use different table/column names, update them to match this schema before deployment.
